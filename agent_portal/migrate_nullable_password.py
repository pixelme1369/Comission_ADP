"""One-off migration: drops the NOT NULL constraint on agent.password_hash in
an existing database. Needed for "Sign in with Google" (see auth.py's
/login/google route) — an agent the admin adds without a temporary password is
Google-sign-in-only, which requires password_hash to be nullable.

db.create_all() only creates tables that don't exist yet — it never alters an
existing table's column constraints (same "no migrations" limitation as the
internal app; see its CLAUDE.md). A fresh database created after this change
already gets the nullable column for free; this script is only needed against
a database that existed before it.

Safe to run more than once and does not touch any existing data — every
existing agent row already has a password_hash set, so dropping the
constraint doesn't change what's stored, only what's allowed going forward.

Usage:
    export DATABASE_URL="postgresql://...neon connection string..."
    python migrate_nullable_password.py
"""

from sqlalchemy import text

from agent_portal import create_app, db


def main():
    app = create_app()
    with app.app_context():
        db.session.execute(text("ALTER TABLE agent ALTER COLUMN password_hash DROP NOT NULL"))
        db.session.commit()
        print("Migration complete: agent.password_hash is now nullable.")


if __name__ == "__main__":
    main()
