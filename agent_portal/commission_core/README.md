# commission_core

Single shared source of truth for commission math, used by BOTH `app/` (the internal
single-user tool at the repo root) and `agent_portal/` (the multi-user portal). Before this
package existed, `calculator.py`, `crm_parser.py`, `cordoba_parser.py`, and
`commission_history_parser.py` were duplicated verbatim between the two apps — a future rule
change (a new tier, a new clawback rule, a bug fix) had to be applied twice by hand, and nothing
enforced that it actually was. Now there is exactly one copy of each; both apps import from here.

## Why this lives inside `agent_portal/`, not at the repo root

The architecturally "obvious" spot is a true sibling of both apps at the repo root
(`Comission_ADP/commission_core/`). That's not where it lives, on purpose: the agent_portal
Vercel project's **Root Directory** setting is `agent_portal/` (see `agent_portal/README.md`).
Vercel's legacy `@vercel/python` builder resolves everything relative to that Root Directory, and
whether a sibling directory *outside* it (like a repo-root `commission_core/`) actually ships in
the deployed bundle depends on a "include files outside the Root Directory" project setting this
session had no way to inspect or verify for the real, deployed Vercel project. Guessing wrong here
means a broken production deploy with a money-math app.

Nesting `commission_core/` inside `agent_portal/` instead sidesteps that risk entirely — it's
already inside the directory Vercel deploys, so it ships with zero Vercel settings changes. `app/`
reaches it via a `sys.path` addition in `app/__init__.py` (see the comment there) — importing
`commission_core` from `app/`'s code works exactly like a repo-root package would, it's just one
extra path entry to get there.

If you later confirm (in the Vercel dashboard, Project Settings → Root Directory → "Include files
outside of the Root Directory in the Build Step") that agent_portal's project has that toggle on,
moving this directory to the repo root and dropping the `app/__init__.py` sys.path shim is safe —
nothing in either app depends on the physical location beyond that one shim and `agent_portal/api/
index.py` already putting `agent_portal/` on `sys.path`.

## What's here

- `calculator.py` — pure commission tier/penalty/bonus/clawback math, no Flask deps.
- `cordoba_parser.py` — reads the Cordoba payout .xlsx (First Pays/EPF/Chargebacks tabs).
- `commission_history_parser.py` — reads a prior account manager's ledger to backfill pre-app
  history.
- `crm_parser.py` — parses the full-history CRM export. This is the ONE file with real behavior
  differences between the two apps, all owner-confirmed and all controlled by parameters on
  `parse_crm_and_calculate()` — see the module docstring at the top of that file for the full
  explanation of `persist_same_month_cancel`, `require_prior_payment_evidence`,
  `require_clawback_payment_evidence`, and `already_history_paid_crm_ids`. Do not fork this file to
  add a new app-specific behavior; add another explicit flag/parameter instead, and document why in
  that same docstring with an owner sign-off reference.

## Tests

`tests/test_commission_core_parity.py` (repo root) asserts both apps' actual call sites produce
identical commission numbers from identical input, other than the documented divergences above —
see that file for what it does and doesn't cover.
`agent_portal/tests/test_commission_history_no_double_pay.py` covers `already_history_paid_crm_ids`
specifically, since it's inherently database-driven rather than a pure parser-input scenario.
