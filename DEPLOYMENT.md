# Deploying to Vercel

## One-time setup

1. **Attach a Postgres database.** In the Vercel project dashboard, add a Postgres
   storage integration (Vercel Postgres / Neon). This sets one of `DATABASE_URL`,
   `POSTGRES_URL`, or `POSTGRES_PRISMA_URL` automatically — `config.py` checks all
   three. Confirm the exact variable name Vercel actually set once the integration
   is attached; the app falls back to local SQLite if none of them are present.

2. **Set environment variables** in the Vercel project settings:
   - `SECRET_KEY` — a real random value (do not use the local dev default).
   - `FLASK_DEBUG=0`
   - The Postgres URL (set automatically by the storage integration — see above).

3. **Create the production schema.** `db.create_all()` only runs locally (guarded by
   `if __name__ == "__main__"` in `run.py`, and never called on Vercel's WSGI import
   path). Instead, run the migration once against the real database:
   ```bash
   export DATABASE_URL="<paste the production Postgres URL>"
   export FLASK_APP=run.py
   flask db upgrade
   ```
   Do this from a machine with network access to the database (e.g. `vercel env pull`
   locally, then run the command above) — do NOT wire this into app startup, since
   serverless cold starts are frequent and concurrent and could race on the same
   migration.

4. **Create the first admin login.** There's no seed script — after step 3, connect
   to the database directly (or run a one-off local Python shell against
   `DATABASE_URL`) and insert the first `AgentUser` row with `is_admin=True`,
   `agent_name=None`, and `password_hash=werkzeug.security.generate_password_hash(...)`.
   Every agent login after that can be created through the `/admin/agents` page.

5. **Deploy.** `vercel.json` routes all traffic to `api/index.py`, which imports the
   Flask `app` object from `run.py`.

## Future schema changes

Local SQLite dev keeps using `db.create_all()` (unchanged, zero extra steps —
delete `instance/commissions.db` and restart if you change a model). Production
Postgres uses Flask-Migrate/Alembic:
```bash
flask db migrate -m "describe the change"
flask db upgrade   # run once, pointed at the production DATABASE_URL
```

## Known gaps / things to verify after first deploy

- **Rate limiting on `/login` is not implemented.** No existing infra for it
  (`Flask-Limiter` isn't installed — would need a backing store since in-memory
  limiting doesn't work across serverless invocations). Revisit if this becomes a
  real concern.
- **Upload size limits.** `MAX_CONTENT_LENGTH` in `config.py` is 5MB, but Vercel's
  serverless functions have their own request body size and execution time limits
  (plan-dependent) separate from that Flask setting. A very large CRM export could
  hit Vercel's ceiling before Flask's. Not solved here — verify with a real upload
  after deploying, and revisit (e.g. chunked upload, background processing) if it's
  a problem in practice.
- **Password resets are manual.** There's no email-sending capability in this app;
  the admin resets a password via `/admin/agents` and relays the new one to the
  agent out-of-band.
