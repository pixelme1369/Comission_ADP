"""One-off migration: lets Commission History backfills and Calculated
Commission Periods coexist for the same month, on an existing database.

Before this change, commission_period.period_label was globally unique, so a
Commission History import for a month that already had a calculated period
(or vice versa) was rejected outright ("Period 2026-01 already exists —
delete it first"). Owner policy (confirmed August 2026): these are two
separate datasets that are allowed to overlap by month — Commission History
is backfilled reference/audit data ("what was actually paid"), Calculated
Commission Periods are this software's own math ("what the software
calculated") — see CommissionPeriod's docstring in models.py for the full
reasoning, and CLAUDE.md's "Commission History vs. Calculated Commission
Periods" note.

This adds a source column (default 'crm', matching every period this
database already has except history backfills — those get corrected to
'history_import' by the UPDATE below, keyed off their child
agent_commission rows' own source) and replaces the single-column unique
constraint on period_label with a composite one on (period_label, source),
so "2026-01"/crm and "2026-01"/history_import can both exist without
conflict, while re-importing the exact same dataset twice is still rejected.

db.create_all() only creates tables that don't exist yet — it never alters
an *existing* table's columns or constraints (same "no migrations"
limitation as the internal app; see its CLAUDE.md, and this portal's own
migrate_nullable_password.py / migrate_widen_client_record_columns.py for
the same pattern applied elsewhere). A fresh database created after this
change already gets the new schema for free; this script is only needed
against a database that existed before it.

Safe to run more than once — everything below is decided from a single
schema snapshot taken up front (not re-checked mid-transaction, which on
Postgres's default read-committed isolation could see stale results for a
connection other than the one running the ALTERs) and every statement is
skipped if it's already been applied. Does not touch any existing
period_label or filename data — only backfills the new source column and
swaps which constraint enforces uniqueness.

Usage:
    export DATABASE_URL="postgresql://...neon connection string..."
    python migrate_split_commission_period_source.py
"""

from sqlalchemy import inspect as sa_inspect, text

from agent_portal import create_app, db


def main():
    app = create_app()
    with app.app_context():
        # Snapshot the schema ONCE, before any ALTER below — introspecting again
        # mid-transaction could see a stale pre-DDL view of the table on a
        # separate pooled connection, since Postgres DDL here isn't visible
        # outside this transaction until it commits.
        cols = {c["name"] for c in sa_inspect(db.engine).get_columns("commission_period")}
        existing_uniques = sa_inspect(db.engine).get_unique_constraints("commission_period")
        old_unique_name = next(
            (uc["name"] for uc in existing_uniques if uc["column_names"] == ["period_label"]), None,
        )
        has_composite_unique = any(
            uc["column_names"] == ["period_label", "source"] for uc in existing_uniques
        )

        if "source" not in cols:
            db.session.execute(text(
                "ALTER TABLE commission_period ADD COLUMN source VARCHAR(20) "
                "NOT NULL DEFAULT 'crm'"
            ))
            print("Added commission_period.source (defaulted existing rows to 'crm').")
        else:
            print("commission_period.source already exists — skipping column add.")

        # Rows that were actually Commission History imports were saved before
        # this column existed, so the blanket 'crm' default above is wrong for
        # them specifically — correct those using what's already true and
        # reliable: their own agent_commission children's source. Safe to run
        # even if the column already existed and this already ran before.
        result = db.session.execute(text(
            "UPDATE commission_period SET source = 'history_import' "
            "WHERE id IN (SELECT DISTINCT period_id FROM agent_commission WHERE source = 'history_import') "
            "AND source != 'history_import'"
        ))
        if result.rowcount:
            print(f"Corrected {result.rowcount} pre-existing history-import period(s) from 'crm' to 'history_import'.")

        if old_unique_name:
            db.session.execute(text(
                f'ALTER TABLE commission_period DROP CONSTRAINT "{old_unique_name}"'
            ))
            print(f"Dropped old single-column unique constraint: {old_unique_name}.")
        else:
            print("No single-column unique constraint on period_label found — skipping drop.")

        if not has_composite_unique:
            db.session.execute(text(
                "ALTER TABLE commission_period ADD CONSTRAINT "
                "uq_commission_period_label_source UNIQUE (period_label, source)"
            ))
            print("Added composite unique constraint on (period_label, source).")
        else:
            print("Composite unique constraint on (period_label, source) already exists — skipping add.")

        db.session.commit()
        print("Migration complete.")


if __name__ == "__main__":
    main()
