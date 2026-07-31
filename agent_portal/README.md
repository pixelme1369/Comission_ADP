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
`calculator.py` and `crm_parser.py` (see "What's vendored" below) so the two apps can evolve
independently.

## What's vendored vs. new

- `agent_portal/calculator.py`, `agent_portal/crm_parser.py` — byte-for-byte copies of the
  business logic in `app/calculator.py` / `app/crm_parser.py` (only the internal import path was
  changed). If the tier table, clawback rules, or classification logic ever change in the main
  app, copy the updated file here too — there's no shared import between the two apps by design.
- Everything else (`models.py`, `auth.py`, `drive_sync.py`, `ingest.py`, `routes_agent.py`,
  `routes_admin.py`, templates) is new, built for this portal.
- **Out of scope for v1**: the Cordoba payout-file ingestion (First Pays / EPF / Chargebacks
  tabs) that the internal app supports is not ported here — this portal only ingests the CRM
  export. `ClientRecord.cordoba_paid` and Cordoba chargeback badges from the internal app are not
  present in this portal's UI.

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

### 3. Deploy to Vercel

1. Import this repo into a new Vercel project, set the **Root Directory** to `agent_portal/`.
2. Set environment variables in the Vercel project settings:
   - `DATABASE_URL` — from step 1
   - `SECRET_KEY` — any long random string (Flask session signing)
   - Nothing Drive-related is needed for the current (Option B) setup — see "Google Drive
     access" above if that ever changes.
3. Deploy. `api/index.py` creates any missing tables on cold start (no migrations, same
   `db.create_all()` pattern as the internal app — see the internal app's own `CLAUDE.md` note on
   this).
4. Log in with an admin account (see below) and use **Import CRM Export** on `/admin` to bring in
   the first CRM export.

### 4. Create the first admin account

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
