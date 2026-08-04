"""One-off migration: widens several client_record VARCHAR columns on an
existing database. Needed because these were originally sized off one
sample CRM row rather than any documented field limit — a real export with
a longer value in one of them (an ID, a verbose Status/Stage string, etc.)
crashed the whole upload with a raw Postgres "value too long for type
character varying(N)" error, with no exception handling around it at the
time, so it surfaced as a plain "Internal Server Error" with no indication
of what actually went wrong.

db.create_all() only creates tables that don't exist yet — it never alters
an *existing* column's type (same "no migrations" limitation as the
internal app; see its CLAUDE.md, and this portal's own
migrate_nullable_password.py for the same pattern applied to a NOT NULL
constraint instead of a length). A fresh database created after this
change already gets the wider columns for free; this script is only
needed against a database that existed before it.

Safe to run more than once — widening a column that's already wide enough
is a no-op — and does not touch any existing data (existing values are
already well within the new limits, since they had to fit the old ones).

Usage:
    export DATABASE_URL="postgresql://...neon connection string..."
    python migrate_widen_client_record_columns.py
"""

from sqlalchemy import text

from agent_portal import create_app, db

# (column, new_length) — must match the widths in models.py's ClientRecord.
COLUMNS = [
    ("crm_id", 100),
    ("stage", 255),
    ("status", 255),
    ("submitted_date", 100),
    ("enrolled_date", 100),
    ("first_payment_date", 100),
    ("first_payment_cleared_date", 100),
    ("second_payment_cleared_date", 100),
    ("dropped_date", 100),
    ("pay_freq", 100),
]


def main():
    app = create_app()
    with app.app_context():
        for column, new_length in COLUMNS:
            db.session.execute(text(
                f"ALTER TABLE client_record ALTER COLUMN {column} TYPE VARCHAR({new_length})"
            ))
        db.session.commit()
        widened = ", ".join(f"{c}({n})" for c, n in COLUMNS)
        print(f"Migration complete: client_record columns widened — {widened}.")


if __name__ == "__main__":
    main()
