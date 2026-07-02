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

**Key files:**
- `app/calculator.py` — pure commission logic, no Flask deps. All tier/penalty/bonus rules live here.
- `app/csv_parser.py` — validates manual CSV columns/types, calls the calculator, returns errors or results
- `app/crm_parser.py` — parses the full-history CRM export, classifies clients, calculates commissions and clawbacks in one pass, returns one dict per period
- `app/models.py` — three tables: `CommissionPeriod`, `AgentCommission`, `ClientRecord`
- `app/routes.py` — routes: `/`, `/upload`, `/upload-crm`, `/period/<id>`, `/period/<id>/agent/<id>`, `/period/<id>/export`, `/period/<id>/agent/<id>/export`, `/period/<id>/delete`, `/history`

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
- Commission vs draw: if `gross_commission > hourly_draw`, agent gets commission; otherwise agent keeps the draw (no repayment required). `hourly_draw` defaults to 0.0 in CRM flow (draw feature not yet wired for CRM uploads).

## Clawback Rules

Commission for a cleared month is **paid on the 25th of the following month** (`_payment_date_for_period` in `crm_parser.py`).

| Scenario | Classification |
|---|---|
| Cleared Month A, dropped before payment date | `same_month_cancel` — never paid, excluded, no clawback |
| Cleared Month A, dropped on/after payment date, payments < threshold | `clawback` — commission already sent, deduct from dropped month |
| Cleared Month A, dropped any time, payments >= threshold | `safe_cancel` — no clawback ever |
| Cleared and dropped same calendar month | `same_month_cancel` — no clawback |

**Safe payment threshold** (from `Pay Freq.` column):
| Pay Freq. | Payments needed to be safe |
|---|---|
| Monthly | 2 |
| Biweekly | 4 |
| Missing / unknown | 3 (legacy fallback) |

Implemented in `_safe_payment_threshold(pay_freq)` in `crm_parser.py`.

**Tier recalculation on clawback:** if removing the cancelled unit drops the agent's tier for the original cleared month, the clawback = full commission difference on all that month's debt (not just the one client's share).

## Client Classification (`crm_parser.py`)

```
cleared          → 1st Payment Cleared Date filled, no Dropped Date, not Pending Affiliate Cancellation
pending          → 1st Payment Cleared Date filled, no Dropped Date, status == "Pending Affiliate Cancellation"
same_month_cancel → cleared and dropped same calendar month, OR dropped before the 25th payout date,
                    OR was pending then cancelled (commission never paid — no clawback)
clawback         → cleared Month A, dropped Month B (on/after payment date), payments_made < 3,
                    AND commission was actually paid (client was in cleared_buckets this file OR
                    crm_id exists as is_cleared=True in DB from a prior upload)
safe_cancel      → cleared Month A, dropped any time, payments_made >= 3
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

## UI Notes

- Notes column on results page shows a pill button; clicking opens a modal with each note as a list item (pipe-delimited in the DB)
- Agent detail page shows all client sections: Cleared, Pending, Clawbacks, Cancelled — each table includes ID, Enrolled Date, and Dropped Date columns
- Agent CSV export includes ID, Enrolled Date, and Dropped Date
