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

## Tests

```bash
python -m pytest tests/ -q
```

The suite locks in every commission business rule (tier boundaries, penalty/bonus thresholds, clawback math, CRM classification, Cordoba chargeback dedup, history import). **Run it after any change to `calculator.py`, `crm_parser.py`, `commission_history_parser.py`, or `routes.py` — a failing test means the money math changed.** Owner-confirmed policies are marked as such in the test docstrings; do not "fix" a failing policy test without owner sign-off.

The manual pre-aggregated CSV upload flow (`/upload`, `csv_parser.py`) was removed at the owner's request in July 2026 — the CRM export flow is the only way to create periods from current data. Do not re-add it.

## Architecture

This is a Flask + SQLAlchemy web app for calculating agent commissions at American Debt Protection.

**Primary upload flow (CRM export):**
1. User uploads a single full-history CRM CSV → `POST /upload-crm` (routes.py)
2. `crm_parser.py` reads every client row, classifies each one, groups by agent + month, computes commissions and clawbacks entirely in-memory
3. Results are saved to SQLite as `CommissionPeriod` + `AgentCommission` + `ClientRecord` rows (one period per calendar month found in the file)
4. User is redirected to `/period/<id>` or `/history` if multiple periods were created

**Cordoba payout check (funder payout confirmation):**
1. User uploads one or more Cordoba payout exports (.xlsx) → `POST /upload-cordoba-payout` (routes.py)
2. `cordoba_parser.py` reads the `First Pays`, `EPF`, and `Chargebacks` tabs. Per the owner
   (July 2026), the only columns that matter are: First Pays → `ID`; Chargebacks → `ID` only
   (`Full Name` is also read, purely to make flash/skip messages readable — not used for any
   decision); EPF → `Contact ID` + `Cleared Date`. Everything else in those tabs — including
   Chargebacks' own `Marketing Payout Debt`, `Dropped Date`, `Payments Made`, etc. — is ignored;
   client debt, the dropped-date used to place the deduction, and payments-made all come from
   OUR OWN `ClientRecord` history instead, never from the Chargebacks file itself.
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
   **Second gate:** the `crm_id` must also appear in the `CordobaPaidClient` ledger (built from
   every First Pays/EPF upload ever processed, not just this file) — i.e. Cordoba must have
   actually confirmed paying us on it at some point. A chargeback logically can't exist without
   a prior payment, so in practice this only catches data gaps (the original payout confirmation
   was never uploaded here), and those are skipped with their own flash message rather than
   clawed back on faith.
   **Third gate (never claw back twice, either direction):** any `crm_id` that already has a
   `ClientRecord` with `clawback_applied=True` — from a CRM upload that reflected the drop, or
   from a commission-history import's "To subtract" row — is skipped with its own flash message.
   Without this, a Cordoba Chargebacks file arriving *after* the CRM export already clawed the
   agent back would deduct the same client a second time (the `CordobaChargedBackClient` ledger
   only guards the Cordoba-first ordering). Regression-tested in
   `tests/test_cordoba_chargebacks.py`.
   **Fourth gate:** we must already have OUR OWN `ClientRecord.dropped_date` for that client (from
   a CRM upload that reflected the drop) — the Chargebacks file's own `Dropped Date` column is
   deliberately never read (owner policy, July 2026). If we don't have a dropped date yet, there's
   nowhere to place the deduction, so the client is skipped with its own flash message telling the
   uploader to upload the CRM export that reflects the drop first, then re-upload this Chargebacks
   file. Client debt for the clawback math is likewise always `ClientRecord.enrolled_debt` — the
   Chargebacks tab's `Marketing Payout Debt` column is never used, even as a fallback; if our own
   enrolled debt is 0, the computed clawback is $0 and nothing is deducted.
   If all checks pass, the agent's commission is clawed back **unconditionally**, regardless of the
   safe-payment-threshold that protects agents in the CRM-driven clawback flow — in practice
   Cordoba stops charging back once an agent-protecting threshold is hit anyway, so no
   threshold check is applied on this path. The clawback amount reuses
   `calculator.calculate_clawback_amount` (same tier-recalculation rule as the CRM flow — see
   Clawback Rules below) and is deducted from the agent's commission in the client's **own
   `ClientRecord.dropped_date` month**, creating a zero-unit `CommissionPeriod`/`AgentCommission`
   holding entry if that month doesn't exist yet (`routes.py::_get_or_create_agent_period_row`,
   mirrors the CRM flow's Step 4). Each `crm_id` is recorded forever in
   `CordobaChargedBackClient` (`crm_id` unique) so re-uploading the same Chargebacks file, or a
   later CRM upload that reflects the same drop, never claws the agent back twice — `crm_parser.py`
   is passed this ledger as `already_charged_back_crm_ids` and skips computing a clawback for any
   `crm_id` already in it.
   **The "Cordoba Clawback" display badge is decoupled from the gates above (OWNER POLICY,
   confirmed July 2026).** Clients hit by a Cordoba chargeback show a red **"Cordoba Clawback:
   Yes"** badge next to the "Cordoba Payout" column, per client, on the agent detail page's
   Cleared Clients table (and in the agent/all-agents CSV exports) — but this reflects a simple
   ID match, not a successful deduction. `routes.py::_mark_cordoba_chargeback_matches` runs
   independently of `_apply_cordoba_chargebacks` and, for every ID in the Chargebacks tab, checks
   it against ALL of our own commission reports (`ClientRecord.crm_id`, any period, any status —
   none of the four gates above apply). Any match is recorded forever in
   `CordobaChargebackMatchedClient` (`crm_id` unique, separate table from
   `CordobaChargedBackClient`), which is what the badge actually looks up. So the badge can — and
   routinely will — show "Yes" for a client where no money was actually deducted yet (e.g. we
   don't have our own Dropped Date on file, or the client was never confirmed paid via First
   Pays/EPF): it's an early-warning flag that Cordoba considers this client charged back, ahead of
   whenever our own data catches up enough to actually move the dollars. A still-cleared client's
   own `ClientRecord` never gets `clawback_applied=True` regardless — that flag only ever lands on
   a separate holding record in the dropped month once the real deduction goes through. There is
   deliberately no period-level dashboard badge for this (owner removed it July 2026) — the
   per-client column is the only place it's shown.
5. **EPF** (unit-credit section — OWNER DECISION, updated July 2026): each EPF row's
   `Contact ID` is matched against our `ClientRecord` history to find the sales rep, and the
   month is taken from the tab's `Cleared Date`. Matches are stored in `EpfClient` (`crm_id`
   unique — re-uploads are no-ops) keyed by `(period_label, agent_name)` — matched by label,
   not FK, so entries apply once that month's period exists regardless of upload order. They
   render as an "EPF" section at the bottom of the agent detail page (below Pending) and as
   `Type=EPF` rows in the agent CSV exports.
   **Each EPF row credits exactly one unit toward the matched agent's tier for that month —
   it can bump `units_cleared` and therefore the tier/rate applied to the agent's OTHER real
   cleared debt that month — but its own enrolled debt is never added to `total_cleared_debt`,
   so an EPF row contributes no commission dollars on its own** (if the agent has no other
   real cleared debt that month, the tier bump is moot and gross commission stays $0).
   `AgentCommission.epf_units` tracks how many of the row's `units_cleared` currently come from
   EPF, so later EPF uploads can recompute idempotently (`units_cleared - epf_units` recovers
   the CRM-only base before re-adding the fresh count) — see
   `routes.py::_recompute_agent_epf_units`. If the month's `CommissionPeriod` doesn't exist yet
   (EPF file arrived before the CRM export), nothing is recomputed at upload time; instead
   `crm_parser.py::parse_crm_and_calculate` is passed `epf_units_by_agent_period` (built from
   every `EpfClient` row in the DB) and folds the credit in when that period is first created,
   so the unit is never lost regardless of ordering. Rows are skipped (with a flash summary)
   when: the client is already commissioned (`is_cleared=True` anywhere — never suggest paying
   twice), the Contact ID isn't in our records, or Cleared Date is missing/unparseable.
   Regression-tested in `tests/test_epf.py` and `tests/test_crm_parser.py::TestEpfUnits`.

**Commission history backfill (pre-app paid history):**
1. User uploads one or more prior account manager ledgers (.xlsx or .csv, NOT a CRM export) + a
   `Year` form field → `POST /upload-commission-history` (routes.py)
2. `commission_history_parser.py` reads the single sheet/file, format: `Month, ID, Sales Rep,
   Full Name, Enrolled Debt, To subtract, Payments Made, Units, Status, Marketing Campaign`.
   `.xlsx` is read via openpyxl (first sheet); `.csv` is read via the stdlib `csv` module —
   dispatched purely on file extension (`_read_rows` in the parser). Both paths converge on the
   same header-lowercased column lookup and row-classification logic below.
   The `Month` column has no year, hence the separate `Year` field — the whole file is assumed
   to be one calendar year.
3. Each row is exactly one of two things (never both, per the source format): **Enrolled Debt**
   filled means the agent was actually paid commission on that client that month; **To
   subtract** filled (Enrolled Debt blank) means a clawback dollar amount the prior manager
   already deducted from the agent that month. The dollar amount on "To subtract" rows is used
   **as-is** — it is not recomputed through `calculate_clawback_amount`, since we don't have
   enough history from this file alone to redo that math accurately.
4. Rows are grouped by `(Month+Year, Sales Rep)` and run through the same
   `calculate_agent_commission` tier math as every other flow, to reconstruct a real
   `CommissionPeriod` + `AgentCommission` + `ClientRecord` (`is_cleared=True` for paid rows) for
   each month — **exactly the same DB shape the CRM flow produces**. This is deliberate: it
   means `_apply_cordoba_chargebacks`'s lookup (`ClientRecord.query.filter_by(crm_id=...,
   is_cleared=True)`) needs zero changes to find these backfilled clients later — a Cordoba
   Chargebacks-tab upload can claw back an agent for a client paid before this app ever existed,
   as long as that client's month has been backfilled this way.
5. Same "period already exists" guard as every other upload flow — a month already present in
   the DB is skipped (with a flash message) rather than double-counted; delete it first to
   re-import.

**Key files:**
- `app/calculator.py` — pure commission logic, no Flask deps. All tier/penalty/bonus rules live here, including `calculate_clawback_amount` (shared by both the CRM-driven and Cordoba-chargeback-driven clawback paths).
- `app/crm_parser.py` — parses the full-history CRM export, classifies clients, calculates commissions and clawbacks in one pass, returns one dict per period
- `app/cordoba_parser.py` — reads the Cordoba payout .xlsx (First Pays / EPF / Chargebacks tabs), returns raw normalized rows; no DB access
- `app/commission_history_parser.py` — reads a prior account manager's ledger .xlsx (not a CRM export) to backfill pre-app commission history; no DB access
- `app/models.py` — `CommissionPeriod`, `AgentCommission`, `ClientRecord`, `CordobaPaidClient`, `CordobaChargedBackClient`, `CordobaChargebackMatchedClient`, `EpfClient`
- `app/routes.py` — routes: `/`, `/upload-crm`, `/upload-cordoba-payout`, `/upload-commission-history`, `/period/<id>`, `/period/<id>/agent/<id>`, `/period/<id>/export`, `/period/<id>/agent/<id>/export`, `/period/<id>/delete`, `/history`

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
- **OWNER POLICY (confirmed July 2026):** a client counted as `clawback` at classification time stays in the cancellation rate **even if** the paid-guard later determines the agent was never paid on them (pending → cancelled) and charges no clawback. An enrolled client who cancelled counts against the agent's quality rate regardless of whether commission ever went out. Do NOT "fix" this by recomputing the rate after reclassification — it is intentional (locked in by `tests/test_crm_parser.py::TestCancellationRatePolicy`).
- Commission vs draw: if `gross_commission > hourly_draw`, agent gets commission; otherwise agent keeps the draw (no repayment required). `hourly_draw` is always 0.0 today — the draw logic lives in `calculator.py` (and is tested) but no upload flow supplies a draw value since the manual CSV flow was removed.

**Per-agent fixed-rate override (OWNER POLICY, confirmed July 2026):** Alex Tambouly has a
fixed **2% rate** negotiated directly with the CEO, outside the standard tier plan. Configured
in `calculator.AGENT_FIXED_RATES` (matched case/whitespace-insensitively via `get_fixed_rate`).
When an agent has a fixed rate: it applies **unconditionally** — the tier table is not consulted
and the cancellation-rate tier-drop penalty never applies, no matter how high the cancellation
rate is. The fixed rate is also reused for that agent's clawback math (`calculate_clawback_amount`
and the flat-rate fallback path) instead of the normal tier-recalculation rule, so a clawed-back
client's rate always matches what the agent was actually paid. Locked in by
`tests/test_calculator.py::TestFixedRateOverride`. Do not "fix" or remove this without owner
sign-off, and do not let it affect any other agent's math.

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

**Second, independent clawback trigger — Cordoba chargebacks:** everything above describes clawbacks detected from the CRM export itself (a Dropped Date appearing in a later CRM upload). A client can also get clawed back because Cordoba's Chargebacks tab shows they took the marketing payout back from the company — see "Cordoba payout check" above. That path skips the safe-payment-threshold table entirely (claws back unconditionally whenever we previously paid the agent) but reuses the same tier-recalculation math (`calculator.calculate_clawback_amount`) and the same "deduct from dropped month" mechanic. **The same client is never clawed back twice, in either order:** Cordoba-first is guarded by the `CordobaChargedBackClient` ledger passed into `crm_parser.py` as `already_charged_back_crm_ids`; CRM-first (or history-import "To subtract"-first) is guarded by the Cordoba flow's third gate, which skips any `crm_id` that already has a `clawback_applied=True` `ClientRecord`.

**Skipped-period clawback warning:** when a CRM upload skips a period because it already exists in the DB, any *new* clawback the parser routed into that month (e.g. a Dropped Date backdated into an already-uploaded month) is NOT applied — the upload flashes an explicit warning naming the agent, client, and amount so it isn't silently lost. Clawbacks already recorded in the DB are excluded from the warning, so routine monthly re-uploads of the full-history file stay quiet.

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

## UI Notes

- Notes column on results page shows a pill button; clicking opens a modal with each note as a list item (pipe-delimited in the DB)
- Agent detail page shows all client sections: Cleared, Pending, Clawbacks, Cancelled — each table includes ID, Enrolled Date, and Dropped Date columns
- Agent CSV export includes ID, Enrolled Date, and Dropped Date
