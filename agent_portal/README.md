# ADP Agent Commission Portal

A standalone Flask app, separate from the internal `app/` tool at the repo root, that lets each
agent log in and see **only their own** commission history — periods, cleared/pending clients,
and clawbacks. Data is pulled automatically once a day from the `Cordoba_ADP` Google Drive folder
(daily full-history CRM export snapshots), run through the same tested commission math as the
internal tool, and stored in Postgres so it survives Vercel's stateless/ephemeral filesystem.

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

### 2. Google Drive access — service account

The Drive sync needs read access to the `Cordoba_ADP` folder without a human clicking "Allow"
each time, so it uses a service account rather than OAuth:

1. In Google Cloud Console, create (or reuse) a project, enable the **Google Drive API**.
2. Create a **Service Account**, then create a JSON key for it and download it.
3. Share the `Cordoba_ADP` Drive folder (or its parent, if easier) with the service account's
   email address (looks like `something@project-id.iam.gserviceaccount.com`) — **Viewer** access
   is enough, read-only.
4. `GOOGLE_SERVICE_ACCOUNT_JSON` = the entire downloaded JSON key file, as a single-line string
   env var.
5. `DRIVE_FOLDER_ID` — defaults to the `Cordoba_ADP` folder found during setup
   (`1YQDdZ1bYTDqgricxO9f_TyDyWredgDzg`); override only if the folder changes.

### 3. Deploy to Vercel

1. Import this repo into a new Vercel project, set the **Root Directory** to `agent_portal/`.
2. Set environment variables in the Vercel project settings:
   - `DATABASE_URL` — from step 1
   - `SECRET_KEY` — any long random string (Flask session signing)
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — from step 2
   - `DRIVE_FOLDER_ID` — from step 2 (optional, has a default)
   - `CRON_SECRET` — any long random string; Vercel Cron sends it back automatically as
     `Authorization: Bearer <value>` when hitting `/cron/sync` — this is what stops that route
     from being an open, unauthenticated "trigger a sync" endpoint
3. Deploy. `api/index.py` creates any missing tables on cold start (no migrations, same
   `db.create_all()` pattern as the internal app — see the internal app's own `CLAUDE.md` note on
   this).
4. Log in with an admin account (see below) and click **Sync Now** on `/admin` to pull the first
   CRM export.

### 4. Create the first admin account

There's no signup page by design (owner-provisioned accounts only). Create the first admin
directly against the database once, e.g. with a one-off Python shell against `DATABASE_URL`:

```python
from agent_portal import create_app, db
from agent_portal.models import Agent

app = create_app()
with app.app_context():
    admin = Agent(email="saman@americandp.com", display_name="Saman", is_admin=True)
    admin.set_password("choose-a-real-password")
    db.session.add(admin)
    db.session.commit()
```

After that, use `/admin/agents` in the browser to create every agent's login and map it to their
exact CRM "Sales Rep" spelling (the CRM export has no stable agent ID, only that name string).

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

`GOOGLE_SERVICE_ACCOUNT_JSON`/`DRIVE_FOLDER_ID` are only needed to exercise the real Drive sync —
use **Manual CSV Import** on `/admin` instead for local testing with a sample CRM export.

## Tests

```bash
cd agent_portal
pytest tests/ -q
```

Includes vendored `calculator.py`/`crm_parser.py` tests (drift guard against the internal app's
business rules) plus new auth-scoping tests confirming one agent's login can never see another
agent's data.
