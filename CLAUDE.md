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

**Cordoba payout check (funder payout confirmation):**
1. User uploads one or more Cordoba payout exports (.xlsx) → `POST /upload-cordoba-payout` (routes.py)
2. `cordoba_parser.py` reads the `First Pays`, `EPF`, and `Chargebacks` tabs
3. **First Pays / EPF** (paid confirmation): checks OUR existing `ClientRecord.crm_id` values
   against the IDs in those two tabs (not the reverse) — any match flips
   `ClientRecord.cordoba_paid = True`, remembered forever in `CordobaPaidClient` (`crm_id` unique)
   so a CRM upload processed *after* the Cordoba file still comes in pre-flagged. The flag never
   flips back to `False`.
   **The bulk update that flips this must NOT filter on `cordoba_paid.is_(False)`** — a row can
   end up `NULL` instead of `False` if it was ever inserted while this column didn't exist on the
   `ClientRecord` model (this actually happened once, during a revert-then-reapply cycle of this
   feature), and `NULL IS False` is never true in SQL, so that filter would silently skip those
   rows forever with no error. Just unconditionally set every matching crm_id to `True`; re-setting
   an already-`True` row is harmless. The column also has `server_default=db.text("0")` now as a
   second line of defense so a fresh table never produces `NULL` rows in the first place.
   Shown as a per-client "Cordoba Payout" Yes/No column on the agent detail page's Cleared
   Clients table (and in that page's CSV export) — purely informational, does not affect tier,
   units, or commission math.
4. **Chargebacks** (agent clawback trigger): the tab has no agent/rep column, so
   `routes.py::_apply_cordoba_chargebacks` cross-references each charged-back `ID` against OUR OWN
   `ClientRecord` history — `ClientRecord.query.filter_by(crm_id=..., is_cleared=True)` — to find
   which agent was actually paid on that client. If none is found (we never recorded the client as
   cleared/commissioned), it's skipped and listed in a flash message — nothing to claw back.
   If found, the agent's commission is clawed back **unconditionally**, regardless of the
   safe-payment-threshold that protects agents in the CRM-driven clawback flow — in practice
   Cordoba stops charging back once an agent-protecting threshold is hit anyway, so no
   threshold check is applied on this path. The clawback amount reuses
   `calculator.calculate_clawback_amount` (same tier-recalculation rule as the CRM flow — see
   Clawback Rules below) and is deducted from the agent's commission in the client's **Dropped
   Date month** from the Chargebacks row, creating a zero-unit `CommissionPeriod`/`AgentCommission`
   holding entry if that month doesn't exist yet (`routes.py::_get_or_create_agent_period_row`,
   mirrors the CRM flow's Step 4). Each `crm_id` is recorded forever in
   `CordobaChargedBackClient` (`crm_id` unique) so re-uploading the same Chargebacks file, or a
   later CRM upload that reflects the same drop, never claws the agent back twice — `crm_parser.py`
   is passed this ledger as `already_charged_back_crm_ids` and skips computing a clawback for any
   `crm_id` already in it.

**Key files:**
- `app/calculator.py` — pure commission logic, no Flask deps. All tier/penalty/bonus rules live here, including `calculate_clawback_amount` (shared by both the CRM-driven and Cordoba-chargeback-driven clawback paths).
- `app/csv_parser.py` — validates manual CSV columns/types, calls the calculator, returns errors or results
- `app/crm_parser.py` — parses the full-history CRM export, classifies clients, calculates commissions and clawbacks in one pass, returns one dict per period
- `app/cordoba_parser.py` — reads the Cordoba payout .xlsx (First Pays / EPF / Chargebacks tabs), returns raw normalized rows; no DB access
- `app/models.py` — `CommissionPeriod`, `AgentCommission`, `ClientRecord`, `CordobaPaidClient`, `CordobaChargedBackClient`
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

**Tier recalculation on clawback:** if removing the cancelled unit drops the agent's tier for the original cleared month, the clawback = full commission difference on all that month's debt (not just the one client's share). If the tier is unchanged, the clawback is just that client's share (`enrolled_debt × orig_rate`). If the agent has no commission result at all for the original cleared month (e.g. they had 0 net cleared units there after other cancels), the clawback falls back to a flat `enrolled_debt × 1%` (lowest tier rate).

Clawbacks are summed per `(agent, dropped_month)` and deducted from the agent's commission in the month the client **dropped**, not the month they cleared (`net_commission = max(0, gross_commission - clawback_amount)`). If the agent has no cleared units in the dropped month, a zero-unit period entry is created just to carry the clawback.

**Second, independent clawback trigger — Cordoba chargebacks:** everything above describes clawbacks detected from the CRM export itself (a Dropped Date appearing in a later CRM upload). A client can also get clawed back because Cordoba's Chargebacks tab shows they took the marketing payout back from the company — see "Cordoba payout check" above. That path skips the safe-payment-threshold table entirely (claws back unconditionally whenever we previously paid the agent) but reuses the same tier-recalculation math (`calculator.calculate_clawback_amount`) and the same "deduct from dropped month" mechanic. The two paths share a dedup guard (`CordobaChargedBackClient` ledger passed into `crm_parser.py` as `already_charged_back_crm_ids`) so the same client is never clawed back twice.

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

**Guarded against a fresh/empty database:** the whole late-activation block is skipped when
`already_cleared_crm_ids` is empty. Without this guard, uploading a multi-month full-history CRM
file for the very first time (or right after `instance/commissions.db` is deleted for a schema
change) has no prior history to check "crm_id not in that set" against — every single client is
"not in the set", so every client whose cleared month isn't the most recent one in the file gets
wrongly reclassified as a late activation and collapsed into the latest period. This actually
happened once: deleting the db, then re-uploading a several-months-wide CRM export, merged every
historical month into one inflated period. Do NOT remove this guard.

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

## UI Notes

- Notes column on results page shows a pill button; clicking opens a modal with each note as a list item (pipe-delimited in the DB)
- Agent detail page shows all client sections: Cleared, Pending, Clawbacks, Cancelled — each table includes ID, Enrolled Date, and Dropped Date columns
- Agent CSV export includes ID, Enrolled Date, and Dropped Date
