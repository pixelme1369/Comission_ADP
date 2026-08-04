# ADP Agent Commission Portal

A standalone Flask app, separate from the internal `app/` tool at the repo root, that lets each
agent log in and see **only their own** commission history — periods, cleared/pending clients,
and clawbacks. Data comes from the same daily CRM export snapshots as the `Cordoba_ADP` Google
Drive folder, run through the same tested commission math as the internal tool, and stored in
Postgres so it survives Vercel's stateless/ephemeral filesystem.

**Current setup: manual CSV import (Option B below).** Someone downloads the day's CRM export
from `Cordoba_ADP` and uploads it on `/admin` — same file, same manual step already used with the
internal app today, just no Google Cloud project or automation involved. The automatic Drive-sync
code path (Option A) is still in the codebase and can be turned on later without any rebuilding —
see "Google Drive access" below.

This app does not read or write anything in `app/` — it vendors its own copies of
`calculator.py`, `crm_parser.py`, and `cordoba_parser.py` (see "What's vendored" below) so the two
apps can evolve independently.

## What's vendored vs. new

- `agent_portal/calculator.py`, `agent_portal/cordoba_parser.py`,
  `agent_portal/commission_history_parser.py` — byte-for-byte copies of the business logic in
  `app/`'s equivalents (only the internal import path was changed). If the tier table, clawback
  rules, or classification logic ever change in the main app, copy the updated file here too —
  there's no shared import between the two apps by design.
- `agent_portal/crm_parser.py` — same as above, with **one intentional divergence**: it also
  saves `SAME_MONTH_CANCEL` clients (dropped the same month they cleared, or dropped before
  their own payout date) as display-only `ClientRecord` rows, so agents can see who dropped
  without any commission ever being paid on them. The internal app computes this classification
  too but never persists it anywhere. This never touches units/debt/rate/commission math — see
  the divergence note at the top of the file. If re-syncing this file from the internal app after
  a future business-rule change there, re-apply this addition (the `same_month_cancel_buckets`
  block) rather than dropping it.
- Everything else (`models.py`, `auth.py`, `drive_sync.py`, `ingest.py`, `cordoba_ingest.py`,
  `history_ingest.py`, `routes_agent.py`, `routes_admin.py`, templates) is new, built for this
  portal.
- **Cordoba payout check is supported**: `/admin` has a "Cordoba Payout Check" upload for the
  First Pays/EPF/Chargebacks `.xlsx` export — same gates and clawback math as the internal app
  (`cordoba_ingest.py`). Agents see the "Cordoba Payout"/"Cordoba Clawback" badges and the
  "Cordoba Charge back" reconciliation table on their own current period.
- **Commission history backfill is supported**: `/admin` has a "Commission History Backfill"
  upload for a prior account manager's ledger (`.xlsx`/`.csv`, not a CRM export) plus a Year
  field. This matters for clawbacks specifically: a client whose original commission was only
  ever recorded in the *internal app's* database doesn't exist in this portal's database at all
  (they're two entirely separate stores), so a later Cordoba chargeback or CRM-reflected drop for
  that client would find nothing to claw back. Backfilling that history into this portal's own DB
  via this upload fixes that.

## One-time setup

### 1. Database — Neon Postgres

1. Create a free project at [neon.tech](https://neon.tech).
2. Copy the connection string (starts with `postgres://` or `postgresql://`) — this is `DATABASE_URL`.

### 2. Google Drive access — two options, both free (Option B is what's currently set up)

**A Google Cloud service account costs nothing** for this use case — creating a project, enabling
the Drive API, and making read-only calls at this volume has no charge and doesn't require a
credit card on file. That said, Option B (no Google Cloud at all) is what's actually configured
right now — nothing below is required unless you decide to switch to Option A later.

**Option A — automatic daily sync (service account, ~10 min one-time setup, free) — not currently used:**

1. Go to [console.cloud.google.com](https://console.cloud.google.com), sign in with any Google
   account, and create a new project (top-left project dropdown → **New Project**). Skip/ignore
   any billing prompt — it's not required for this.
2. **APIs & Services → Library** → search **Google Drive API** → **Enable**.
3. **APIs & Services → Credentials → Create Credentials → Service account**. Give it any name
   (e.g. `agent-portal-drive-sync`), skip the optional role/access steps, **Done**.
4. Click into the new service account → **Keys** tab → **Add Key → Create new key → JSON** —
   this downloads a `.json` file. Its full contents (as-is) becomes the `GOOGLE_SERVICE_ACCOUNT_JSON`
   env var in Vercel.
5. Note the service account's email, shown on its details page — looks like
   `agent-portal-drive-sync@your-project-id.iam.gserviceaccount.com`.
6. In Google Drive, open the `Cordoba_ADP` folder → **Share** → paste that email → set role
   **Viewer** → Share (you can uncheck "notify," it's not a real inbox).
7. `DRIVE_FOLDER_ID` defaults to the `Cordoba_ADP` folder already found
   (`1YQDdZ1bYTDqgricxO9f_TyDyWredgDzg`) — no action needed unless the folder changes.

Turning this on later also means adding back a `crons` block to `vercel.json` (removed for now
since Option B doesn't need it):
```json
"crons": [{ "path": "/cron/sync", "schedule": "0 12 * * *" }]
```
and setting `CRON_SECRET` (any long random string — Vercel Cron sends it back automatically as
`Authorization: Bearer <value>`, which is what stops `/cron/sync` from being an open,
unauthenticated endpoint). Once `GOOGLE_SERVICE_ACCOUNT_JSON` is set, the admin dashboard
automatically shows a **Sync Now** button too — it's hidden while unconfigured.

**Option B — skip Google Cloud entirely (zero setup, fully manual, free) — this is what's set up:**

`GOOGLE_SERVICE_ACCOUNT_JSON` is left unset. Instead, use **Import CRM Export** on `/admin` —
download the day's CRM export from the `Cordoba_ADP` folder yourself (exactly the same manual
step already used with the internal app today) and upload it there. No Google Cloud project, no
service account, no automation — just a daily click. Everything else in the portal (login,
per-agent scoping, dashboards) works exactly the same either way.

### 3. "Sign in with Google" (optional, lets agents use their company Google account)

The login page shows a **Sign in with Google** button whenever `GOOGLE_OAUTH_CLIENT_ID` is set —
it's simply hidden otherwise, so this is fully optional and every step below can be skipped if
email/password login is enough. This uses Google Identity Services' ID-token button, so unlike
"Option A" Drive access above, **no service account, no client secret, and no Google Cloud billing
project state is needed** — only a public OAuth Client ID.

1. Go to [console.cloud.google.com](https://console.cloud.google.com) (same or a different project
   from the Drive one — doesn't matter) → **APIs & Services → Credentials → Create Credentials →
   OAuth client ID**.
2. If prompted, configure the **OAuth consent screen** first (External or Internal, whichever your
   Google Workspace setup requires) — app name and support email are enough, no scopes needed
   beyond the default "email" and "profile."
3. Application type: **Web application**. Under **Authorized JavaScript origins**, add every
   origin the portal is served from, e.g. `https://your-app.vercel.app` and, for local dev,
   `http://127.0.0.1:5000`. No **Authorized redirect URIs** are needed — this flow doesn't redirect.
4. Copy the **Client ID** (looks like `123456789-abc123.apps.googleusercontent.com`) — this becomes
   the `GOOGLE_OAUTH_CLIENT_ID` env var. The **Client secret** Google also generates is not used
   anywhere and does not need to be copied.
5. In `/admin/agents`, create (or edit) an agent and leave the password field blank — that agent
   can now only sign in via the Google button, using the exact email address entered there.
   Agents that already have a password keep working with either method. There's no domain
   allow-list — an agent's email being listed in `/admin/agents` at all is what grants access, so
   anyone the admin hasn't added is refused even with a legitimate, verified Google account.
6. **Existing deployments only:** the `agent.password_hash` column was originally `NOT NULL`.
   `db.create_all()` never alters an existing table's constraints, so run
   `python migrate_nullable_password.py` once against `DATABASE_URL` (see the script for details)
   — otherwise saving a new password-less agent will fail with a database error. A brand new
   database created after this change doesn't need this step.

### 4. Deploy to Vercel

1. Import this repo into a new Vercel project, set the **Root Directory** to `agent_portal/`.
2. Set environment variables in the Vercel project settings:
   - `DATABASE_URL` — from step 1
   - `SECRET_KEY` — any long random string (Flask session signing)
   - `GOOGLE_OAUTH_CLIENT_ID` — only if you set up "Sign in with Google" above
   - Nothing Drive-related is needed for the current (Option B) setup — see "Google Drive
     access" above if that ever changes.
3. Deploy. `api/index.py` creates any missing tables on cold start (no migrations, same
   `db.create_all()` pattern as the internal app — see the internal app's own `CLAUDE.md` note on
   this).
4. Log in with an admin account (see below) and use **Import CRM Export** on `/admin` to bring in
   the first CRM export.

### 5. Create the first admin account

There's no signup page by design (owner-provisioned accounts only). Run `create_admin.py` once,
locally, pointed at the same `DATABASE_URL` Vercel uses:

```bash
cd agent_portal
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL="postgresql://...your Neon connection string..."
python create_admin.py
```

It prompts for email, display name, password, and whether the account is an admin — answer
"y" for the first one. After that, use `/admin/agents` in the browser (logged in as that admin)
to create every agent's login and map it to their exact CRM "Sales Rep" spelling (the CRM export
has no stable agent ID, only that name string) — `create_admin.py` can also be re-run for any
agent you'd rather provision from the command line instead of the browser form.

## Schema changes (no migration framework)

Same limitation as the internal app: `db.create_all()` only creates tables that don't exist
yet — it never alters an *existing* table to add a new column. Adding a new model is fine (a
fresh table just gets created on the next cold start), but adding a column to an existing model
needs a one-off script run against `DATABASE_URL`, same pattern as `create_admin.py` — see
`migrate_add_cordoba_paid.py` for a real example (adds `client_record.cordoba_paid` without
touching any existing rows). If a future change adds another column, write a similar `ALTER
TABLE ... ADD COLUMN IF NOT EXISTS ...` script rather than assuming a redeploy will pick it up.
Column *constraint* changes need the same treatment — `migrate_nullable_password.py` drops the
`NOT NULL` constraint `agent.password_hash` was originally created with, needed for Google-only
agent accounts (see "Sign in with Google" above). Column *type/length* changes are no different —
`migrate_widen_client_record_columns.py` widens several `client_record` VARCHAR columns that were
originally sized off one sample CRM row; a real export with a longer value in one of them (an ID,
a verbose Status/Stage string, etc.) failed the whole import with a raw Postgres "value too long"
error. All three of these one-off migrations also have a matching **"Fix Now" button on the admin
dashboard** (shown only when the live database's schema is actually out of date, via a runtime
`sa_inspect` check) — running the script by hand against `DATABASE_URL` is only needed if you'd
rather not use the browser.

## Local development

```bash
cd agent_portal
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL="sqlite:///agent_portal_dev.db"   # or a local/dev Postgres
export SECRET_KEY="dev"
export FLASK_APP=api/index.py
flask run
```

Use **Import CRM Export** on `/admin` to load a sample CRM export locally.

## Tests

```bash
cd agent_portal
pytest tests/ -q
```

Includes vendored `calculator.py`/`crm_parser.py` tests (drift guard against the internal app's
business rules) plus new auth-scoping tests confirming one agent's login can never see another
agent's data.
