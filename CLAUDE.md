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

## Architecture

This is a Flask + SQLAlchemy web app for calculating agent commissions at American Debt Protection. There is no test suite yet.

**Request flow:**
1. User uploads a CSV → `POST /upload` (routes.py)
2. `csv_parser.py` validates and parses the file, calls `calculator.py` per row
3. Results are saved to SQLite as a `CommissionPeriod` + `AgentCommission` rows
4. User is redirected to `/period/<id>` to view results

**Key files:**
- `app/calculator.py` — pure commission logic, no Flask deps. All business rules live here.
- `app/csv_parser.py` — validates CSV columns/types, calls the calculator, returns errors or results
- `app/models.py` — two tables: `CommissionPeriod` (one per upload) and `AgentCommission` (one per agent per period)
- `app/routes.py` — five routes: `/`, `/upload`, `/period/<id>`, `/period/<id>/export`, `/period/<id>/delete`, `/history`

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
- Cancellation rate **< 10%** flags `quality_bonus_eligible = True` — this is display-only, not auto-paid
- Commission vs draw: if `gross_commission > hourly_draw`, agent gets commission; otherwise agent keeps the draw (no repayment required)

## CSV Format

One period per file. Required columns (order-independent):

```
agent_name, units_cleared, total_cleared_debt, cancellation_rate, hourly_draw, period
```

- `cancellation_rate`: percentage as a float (e.g. `18.5` = 18.5%)
- `period`: `YYYY-MM` format, must be consistent across all rows
- Duplicate agent names within the same period are rejected
- Uploading a period that already exists in the DB is blocked — user must delete it first

A sample CSV is at `app/static/sample.csv`.
