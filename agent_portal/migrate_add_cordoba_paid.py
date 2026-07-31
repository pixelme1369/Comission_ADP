"""One-off migration: adds the cordoba_paid column to an existing client_record
table. Needed because db.create_all() only creates tables that don't exist yet
— it never alters an existing table to add a new column (same "no migrations"
limitation as the internal app; see its CLAUDE.md). Safe to run more than
once (IF NOT EXISTS) and does not touch any existing data.

Usage:
    export DATABASE_URL="postgresql://...neon connection string..."
    python migrate_add_cordoba_paid.py
"""

from sqlalchemy import text

from agent_portal import create_app, db


def main():
    app = create_app()
    with app.app_context():
        db.session.execute(text(
            "ALTER TABLE client_record ADD COLUMN IF NOT EXISTS cordoba_paid "
            "BOOLEAN DEFAULT FALSE NOT NULL"
        ))
        db.session.commit()
        print("Migration complete: client_record.cordoba_paid now exists.")


if __name__ == "__main__":
    main()
