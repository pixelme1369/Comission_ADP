# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup & Running

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py          # starts Flask dev server at http://127.0.0.1:5000
```

The SQLite database (`instance/commissions.db`) is created automatically on first run via `db.create_all()` in `run.py`.

If you change models, delete `instance/commissions.db` and restart — there are no migrations.

## Architecture

This is a Flask + SQLAlchemy web app for calculating agent commissions at American Debt Protection. There is no test suite yet.

**Primary upload flow (CRM export):**
1. User uploads a single full-history CRM CSV → `POST /upload-crm` (routes.py)
2. `crm_parser.py` reads every client row, classifies each one, groups by agent + month, computes commissions and clawbacks entirely in-memory
3. Results are saved to SQLite as `CommissionPeriod` + `AgentCommission` + `ClientRecord` rows (one period per calendar month found in the file)
4. User is redirected to `/period/<id>` or `/history` if multiple periods were created

**Manual upload flow (fallback):**
1. User uploads a pre-aggregated CSV → `POST /upload`
2. `csv_parser.py` validates and calls `calculator.py` per row
3. Saves `CommissionPeriod` + `AgentCommission` rows (no `ClientRecord` rows)

**Cordoba payout reconciliation flow (funder payout check):**
1. User uploads Cordoba's weekly payout export (.xlsx) → `POST /upload-cordoba-payout` (routes.py)
2. `cordoba_parser.py` reads the `First Pays`, `EPF`, and `Chargebacks` tabs
3. Checks OUR existing commission data against Cordoba's data (not the reverse) — matches by `ID` / `Contact ID` against `ClientRecord.crm_id`
4. See "Cordoba Payout Reconciliation" section below for full details

**Key files:**
- `app/calculator.py` — pure commission logic, no Flask deps. All tier/penalty/bonus rules live here, plus `get_adjusted_rate`/`calculate_clawback_delta` (shared tier-delta math used by the Cordoba reconciliation flow).
- `app/csv_parser.py` — validates manual CSV columns/types, calls the calculator, returns errors or results
- `app/crm_parser.py` — parses the full-history CRM export, classifies clients, calculates commissions and clawbacks in one pass, returns one dict per period
- `app/cordoba_parser.py` — reads the Cordoba weekly payout .xlsx (First Pays / EPF / Chargebacks tabs), returns raw normalized rows; no DB access
- `app/models.py` — `CommissionPeriod`, `AgentCommission`, `ClientRecord`, `CordobaPaidClient`, `CordobaChargeback`
- `app/routes.py` — routes: `/`, `/upload`, `/upload-crm`, `/upload-cordoba-payout`, `/period/<id>`, `/period/<id>/agent/<id>`, `/period/<id>/export`, `/period/<id>/agent/<id>/export`, `/period/<id>/delete`, `/history`

## Commission Business Rules (April 2026 Plan)

The tier table in `calculator.py` must match exactly:

| Units Cleared | Tier | Rate |
|---|---|---|
| 1–20 | 1 | 1.00% |
| 21–31 | 2 | 1.25% |
| 32–39 | 3 | 1.50% |
| 40–45 | 4 | 1.75% |
| 46–60 | 5 | 2.00% |
| 61+ | 6 | 2.25% |

- **60 units = Tier 5** (upper bound inclusive); 61+ = Tier 6
- Cancellation rate **> 20%** (strict) drops one tier; exactly 20% does not trigger penalty
- Cancellation rate **< 10%** flags `quality_bonus_eligible = True` — display-only, not auto-paid
- **Cancel rate formula:** clawback clients ÷ (cleared + clawback clients). Same-month cancels, safe cancels, and pending clients are excluded from both numerator and denominator.
- Commission vs draw: if `gross_commission > hourly_draw`, agent gets commission; otherwise agent keeps the draw (no repayment required). `hourly_draw` defaults to 0.0 in CRM flow (draw feature not yet wired for CRM uploads).

## Clawback Rules

Commission for a cleared month is **paid on the 25th of the following month** (`_payment_date_for_period` in `crm_parser.py`).

Checked in this order — the payments-made safe threshold is evaluated before the payout-date check:

| Scenario | Classification |
|---|---|
| Cleared and dropped same calendar month | `same_month_cancel` — no clawback |
| Cleared Month A, dropped any time, payments >= threshold | `safe_cancel` — no clawback ever, even if dropped before the payout date |
| Cleared Month A, dropped before payment date, payments < threshold | `same_month_cancel` — never paid, excluded, no clawback |
| Cleared Month A, dropped on/after payment date, payments < threshold | `clawback` — commission already sent, deduct from dropped month |

**Safe payment threshold** (from `Pay Freq.` column):
| Pay Freq. | Payments needed to be safe |
|---|---|
| Monthly | 2 |
| Biweekly | 4 |
| Missing / unknown | 3 (legacy fallback) |

Implemented in `_safe_payment_threshold(pay_freq)` in `crm_parser.py`. Also applies to clients still marked "Pending Affiliate Cancellation": if they've already hit the safe threshold, they're classified as `cleared` instead of held in `pending`.

**Tier recalculation on clawback:** if removing the cancelled unit drops the agent's tier for the original cleared month, the clawback = full commission difference on all that month's debt (not just the one client's share).

## Client Classification (`crm_parser.py`)

```
cleared          → 1st Payment Cleared Date filled, no Dropped Date, not Pending Affiliate Cancellation,
                    OR still Pending Affiliate Cancellation but payments_made already hit the safe threshold
pending          → 1st Payment Cleared Date filled, no Dropped Date, status == "Pending Affiliate Cancellation",
                    payments_made below the safe threshold
same_month_cancel → cleared and dropped same calendar month, OR dropped before the 25th payout date
                    without hitting the safe threshold, OR was pending then cancelled (commission never
                    paid — no clawback)
clawback         → cleared Month A, dropped Month B (on/after payment date), payments_made below the
                    safe threshold, AND commission was actually paid (client was in cleared_buckets this
                    file OR crm_id exists as is_cleared=True in DB from a prior upload)
safe_cancel      → cleared Month A, dropped any time, payments_made >= safe threshold (see Safe payment
                    threshold table above)
not_yet_cleared  → no 1st Payment Cleared Date (skipped entirely)
late_activation  → was pending in cleared month (crm_id never in DB as is_cleared), now active,
                    cleared_period < latest period in file → commission credited in latest period
```

## Late Activation Logic

When a client was "Pending Affiliate Cancellation" in their cleared month and later becomes active:
- Commission was never paid in their original cleared month
- On next upload, `parse_crm_and_calculate` receives `already_cleared_crm_ids` (set of crm_ids
  saved as `is_cleared=True` in DB) from `routes.py`
- If client's `crm_id` not in that set AND `cleared_period < latest_period` in file → late activation
- Their `cleared_period` is reassigned to `latest_period` BEFORE bucket-building (Step 1)
- This means the tier for the latest period is recalculated including the late activation client
- `ClientRecord` stores `is_late_activation=True` and `original_cleared_period` for display

## Clawback Guard (Pending → Cancelled)

If a client goes from "Pending Affiliate Cancellation" directly to cancelled (never became active):
- Commission was never paid → must NOT trigger a clawback
- Before calculating any clawback, parser checks:
  1. Was the client in `cleared_buckets` for their cleared month in this file? (first upload case)
  2. Is their `crm_id` in `already_cleared_crm_ids` from DB? (prior upload case)
- If neither → reclassified as `same_month_cancel` → excluded, no clawback

## CRM Required Columns

```
Sales Rep, 1st Payment Cleared Date, Dropped Date, Status, Enrolled Debt, # NSF
```

Optional columns stored in `ClientRecord`: ID, Full Name, Email, Home Phone, Stage, Submitted Date, Enrolled Date, 1st Payment Date, 2nd Payment Cleared Date, Payments Made.

## Manual CSV Format

One period per file. Required columns (order-independent):

```
agent_name, units_cleared, total_cleared_debt, cancellation_rate, hourly_draw, period
```

- `cancellation_rate`: percentage as a float (e.g. `18.5` = 18.5%)
- `period`: `YYYY-MM` format, must be consistent across all rows
- Duplicate agent names within the same period are rejected
- Uploading a period that already exists in the DB is blocked — delete it first

A sample CSV is at `app/static/sample.csv`.

## Cordoba Payout Reconciliation

Cordoba is the funder — they pay us, and we only owe an agent commission on a file once
Cordoba has actually paid us for it. The user uploads Cordoba's weekly payout export
(.xlsx with `First Pays`, `EPF`, and `Chargebacks` tabs) via the "Upload Cordoba Payout"
card on the index page. Matching is always **ID-based** (`ID` / `Contact ID` columns in
Cordoba's file == `ClientRecord.crm_id`), never by agent name — Cordoba's file has no
concept of our internal agents, only the marketing company.

**First Pays / EPF tabs → `cordoba_paid` flag (one-time, ever-funded):**
- Any client ID appearing in either tab is remembered forever in `CordobaPaidClient`
  (so a CRM upload processed *after* the Cordoba file still comes in already flagged).
- `ClientRecord.cordoba_paid` is flipped `True` (never back to `False`) for any existing
  client record matching that ID, regardless of which period it's in.
- This is purely informational — it does NOT change tier, units, or which files count
  as cleared. Shown as "20/23" on the agent's row in `results.html` (cleared units that
  are Cordoba-paid, out of total cleared units that period) and as a per-file Yes/No
  badge on `agent_detail.html`'s Cleared Clients table.

**Chargebacks tab → `CordobaChargeback` (separate from the CRM-predicted clawback):**
- Kept in its own column/table, never merged into `AgentCommission.clawback_amount` /
  `ClientRecord.clawback_amount` (which come from the CRM export's own dropped-date
  logic) — the two are shown side by side so discrepancies are visible, not silently
  reconciled.
- Target period (which month's report gets the deduction) = the month of the
  **Marketing Payment Chargeback** date column, NOT the Dropped Date.
- Agent is resolved by looking up `ClientRecord.query.filter_by(crm_id=..., is_cleared=True)`
  and using ITS `agent_commission` relationship — this also gives the correct original
  period/units/debt/commission to recalculate against (handles late-activation
  reassignment automatically, since it uses what was actually credited, not the raw
  "1st Payment Cleared Date" from Cordoba's file).
- Dollar amount uses `Marketing Payout Debt` (not our stored `enrolled_debt`) run through
  `calculate_clawback_delta()` in `calculator.py` — same tier-drop-recalculation rule as
  the CRM-native clawback.
- If no `ClientRecord` matches the ID, the row is stored with `matched=False` and skipped
  from all totals (nothing to reconcile against yet — check the ID / upload the CRM data first).
- If the matched agent has zero cleared units in the target month, `_get_or_create_holding_agent_commission()`
  in `routes.py` creates a zero-unit `CommissionPeriod`/`AgentCommission` (`source="cordoba"`)
  so the deduction still has somewhere to display.
- Each `crm_id` is only ever recorded once in `CordobaChargeback` (unique constraint) —
  re-uploading an overlapping weekly file won't double-count the same chargeback.

## UI Notes

- Notes column on results page shows a pill button; clicking opens a modal with each note as a list item (pipe-delimited in the DB)
- Agent detail page shows all client sections: Cleared, Pending, Clawbacks, Cancelled — each table includes ID, Enrolled Date, and Dropped Date columns
- Agent CSV export includes ID, Enrolled Date, and Dropped Date
