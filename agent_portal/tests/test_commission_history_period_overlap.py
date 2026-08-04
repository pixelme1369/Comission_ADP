"""Commission History and Calculated Commission Periods are two separate
datasets that are allowed to overlap by month (owner policy, confirmed
August 2026 — see CommissionPeriod's docstring in models.py and CLAUDE.md).

Before this fix, commission_period.period_label was globally unique, so
importing Commission History for a month that already had a calculated
period (or vice versa) was rejected outright: "Period 2026-01 already
exists — delete it first before re-importing." That blocked history imports
for no good reason (a calculated period existing doesn't mean the SAME
month's actual historical payout has been recorded anywhere), and — the
less obvious direction — blocked a genuine CRM upload too, if History for
that month happened to be imported first.

This file proves:
  - A Commission History import is never blocked by an existing calculated
    period for the same month, and vice versa.
  - Neither upload ever overwrites or deletes the other's data.
  - Both show up as their own, clearly separate rows on the dashboard.
  - Re-importing the exact same Commission History file for a month that's
    already been imported is still a no-op (idempotency is preserved —
    only the CROSS-dataset block was removed).
"""

import csv
import io

from agent_portal.models import Agent, AgentCommission, ClientRecord, CommissionPeriod


def _make_admin(db, email="admin@company.com"):
    agent = Agent(email=email, display_name="Admin User", is_admin=True)
    agent.set_password("pw12345")
    db.session.add(agent)
    db.session.commit()
    return agent


def _login_as(client, agent):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(agent.id)
        sess["_fresh"] = True


CSV_HEADERS = [
    "ID", "Sales Rep", "Full Name", "1st Payment Cleared Date", "Dropped Date",
    "Status", "Enrolled Debt", "# NSF", "Payments Made", "Pay Freq.",
]


def _crm_csv_bytes(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_HEADERS)
    writer.writeheader()
    for r in rows:
        writer.writerow({h: r.get(h, "") for h in CSV_HEADERS})
    return out.getvalue().encode("utf-8")


def _history_csv_bytes(rows):
    out = io.StringIO()
    out.write("Month,ID,Sales Rep,Full Name,Enrolled Debt,To subtract,Payments Made\n")
    for r in rows:
        out.write(f"{r['month']},{r['id']},{r['agent']},{r.get('name', '')},"
                   f"{r.get('debt', '')},{r.get('subtract', '')},{r.get('payments', '')}\n")
    return out.getvalue().encode("utf-8")


class TestHistoryImportNeverBlockedByAnExistingCalculatedPeriod:
    def test_history_import_succeeds_for_a_month_that_already_has_a_calculated_period(self, app, db, client):
        admin = _make_admin(db)
        _login_as(client, admin)

        # A real CRM upload creates a calculated ("crm") period for 2026-01 first.
        crm_bytes = _crm_csv_bytes([{
            "ID": "111", "Sales Rep": "Agent A", "Full Name": "CRM Client",
            "1st Payment Cleared Date": "01/10/2026", "Status": "Active",
            "Enrolled Debt": "10000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly",
        }])
        client.post("/admin/upload-csv", data={"csv_file": (io.BytesIO(crm_bytes), "crm.csv")},
                    content_type="multipart/form-data")

        with app.app_context():
            assert CommissionPeriod.query.filter_by(period_label="2026-01", source="crm").count() == 1

        # Commission History for the SAME month must not be blocked by that.
        history_bytes = _history_csv_bytes([
            {"month": "January", "id": "999", "agent": "Agent B", "name": "History Client", "debt": "20000"},
        ])
        resp = client.post(
            "/admin/upload-commission-history",
            data={"history_year": "2026", "history_file": (io.BytesIO(history_bytes), "history.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"already exists" not in resp.data
        assert b"1 month(s) backfilled" in resp.data

        with app.app_context():
            periods = CommissionPeriod.query.filter_by(period_label="2026-01").order_by(
                CommissionPeriod.source).all()
            assert [p.source for p in periods] == ["crm", "history_import"]
            # The calculated period is untouched — still exactly the one CRM client.
            crm_period = next(p for p in periods if p.source == "crm")
            assert ClientRecord.query.filter_by(period_id=crm_period.id).count() == 1
            # The historical period holds its own, separate data.
            history_period = next(p for p in periods if p.source == "history_import")
            assert ClientRecord.query.filter_by(period_id=history_period.id).count() == 1

    def test_crm_upload_succeeds_for_a_month_that_already_has_a_history_import(self, app, db, client):
        admin = _make_admin(db)
        _login_as(client, admin)

        # Commission History for 2026-02 is imported first.
        history_bytes = _history_csv_bytes([
            {"month": "February", "id": "222", "agent": "Agent A", "name": "History Client", "debt": "15000"},
        ])
        client.post(
            "/admin/upload-commission-history",
            data={"history_year": "2026", "history_file": (io.BytesIO(history_bytes), "history.csv")},
            content_type="multipart/form-data",
        )
        with app.app_context():
            assert CommissionPeriod.query.filter_by(period_label="2026-02", source="history_import").count() == 1

        # A real CRM upload for the SAME month must not be blocked by that.
        crm_bytes = _crm_csv_bytes([{
            "ID": "333", "Sales Rep": "Agent B", "Full Name": "CRM Client",
            "1st Payment Cleared Date": "02/05/2026", "Status": "Active",
            "Enrolled Debt": "12000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly",
        }])
        resp = client.post(
            "/admin/upload-csv", data={"csv_file": (io.BytesIO(crm_bytes), "crm.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"already exists" not in resp.data
        assert b"Imported 1 period(s)" in resp.data

        with app.app_context():
            periods = CommissionPeriod.query.filter_by(period_label="2026-02").order_by(
                CommissionPeriod.source).all()
            assert [p.source for p in periods] == ["crm", "history_import"]

    def test_dashboard_shows_both_as_separate_rows_for_the_same_month(self, app, db, client):
        admin = _make_admin(db)
        _login_as(client, admin)

        crm_bytes = _crm_csv_bytes([{
            "ID": "1", "Sales Rep": "Agent A", "Full Name": "CRM Client",
            "1st Payment Cleared Date": "03/10/2026", "Status": "Active",
            "Enrolled Debt": "10000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly",
        }])
        client.post("/admin/upload-csv", data={"csv_file": (io.BytesIO(crm_bytes), "crm.csv")},
                    content_type="multipart/form-data")
        history_bytes = _history_csv_bytes([
            {"month": "March", "id": "2", "agent": "Agent B", "name": "History Client", "debt": "5000"},
        ])
        client.post(
            "/admin/upload-commission-history",
            data={"history_year": "2026", "history_file": (io.BytesIO(history_bytes), "history.csv")},
            content_type="multipart/form-data",
        )

        resp = client.get("/admin/")
        assert resp.status_code == 200
        page = resp.data.decode()
        # Both tables show this month — one calculated row, one historical row.
        crm_section = page.split("Recent CRM Export Uploads")[1].split("Recent Cordoba")[0]
        history_section = page.split("Recent Commission History Uploads")[1]
        assert "2026-03" in crm_section
        assert "2026-03" in history_section

    def test_deleting_the_calculated_period_leaves_the_historical_one_intact(self, app, db, client):
        admin = _make_admin(db)
        _login_as(client, admin)

        crm_bytes = _crm_csv_bytes([{
            "ID": "1", "Sales Rep": "Agent A", "Full Name": "CRM Client",
            "1st Payment Cleared Date": "04/10/2026", "Status": "Active",
            "Enrolled Debt": "10000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly",
        }])
        client.post("/admin/upload-csv", data={"csv_file": (io.BytesIO(crm_bytes), "crm.csv")},
                    content_type="multipart/form-data")
        history_bytes = _history_csv_bytes([
            {"month": "April", "id": "2", "agent": "Agent B", "name": "History Client", "debt": "5000"},
        ])
        client.post(
            "/admin/upload-commission-history",
            data={"history_year": "2026", "history_file": (io.BytesIO(history_bytes), "history.csv")},
            content_type="multipart/form-data",
        )

        with app.app_context():
            crm_period = CommissionPeriod.query.filter_by(period_label="2026-04", source="crm").one()
            crm_period_id = crm_period.id

        client.post(f"/admin/period/{crm_period_id}/delete")

        with app.app_context():
            assert CommissionPeriod.query.filter_by(period_label="2026-04", source="crm").count() == 0
            history_period = CommissionPeriod.query.filter_by(
                period_label="2026-04", source="history_import").one()
            assert ClientRecord.query.filter_by(period_id=history_period.id).count() == 1

    def test_reimporting_the_same_history_month_is_still_a_no_op(self, app, db, client):
        """The CROSS-dataset block was removed; re-importing the SAME
        historical dataset for a month already backfilled is still rejected
        — this file/upload flow's own idempotency guard is untouched."""
        admin = _make_admin(db)
        _login_as(client, admin)

        history_bytes = _history_csv_bytes([
            {"month": "May", "id": "1", "agent": "Agent A", "name": "History Client", "debt": "5000"},
        ])
        client.post(
            "/admin/upload-commission-history",
            data={"history_year": "2026", "history_file": (io.BytesIO(history_bytes), "h1.csv")},
            content_type="multipart/form-data",
        )
        resp = client.post(
            "/admin/upload-commission-history",
            data={"history_year": "2026", "history_file": (io.BytesIO(history_bytes), "h2.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"already imported" in resp.data

        with app.app_context():
            assert CommissionPeriod.query.filter_by(period_label="2026-05", source="history_import").count() == 1


class TestCordobaClawbackStillTargetsTheCalculatedPeriodOnly:
    """_get_or_create_agent_period_row (the Cordoba chargeback deduction path)
    must always create/find the source="crm" period for a label, never the
    separate source="history_import" one, even when a history period for
    that same label already exists."""

    def test_a_history_only_month_still_gets_its_own_crm_period_for_a_cordoba_clawback(self, app, db, client):
        from agent_portal.cordoba_ingest import _get_or_create_agent_period_row

        admin = _make_admin(db)
        _login_as(client, admin)

        history_bytes = _history_csv_bytes([
            {"month": "June", "id": "1", "agent": "Agent A", "name": "History Client", "debt": "5000"},
        ])
        client.post(
            "/admin/upload-commission-history",
            data={"history_year": "2026", "history_file": (io.BytesIO(history_bytes), "h.csv")},
            content_type="multipart/form-data",
        )

        with app.app_context():
            history_period = CommissionPeriod.query.filter_by(
                period_label="2026-06", source="history_import").one()

            period, agent_row = _get_or_create_agent_period_row("2026-06", "Agent A", "chargebacks.xlsx")
            db.session.commit()

            assert period.source == "crm"
            assert period.id != history_period.id
            # The historical period's own data is untouched.
            assert ClientRecord.query.filter_by(period_id=history_period.id).count() == 1
