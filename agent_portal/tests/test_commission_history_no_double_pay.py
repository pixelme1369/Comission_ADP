"""Follow-up to test_commission_history_period_overlap.py's fix.

Once Commission History and Calculated Commission Periods were allowed to
share a month, a real gap opened up: a full CRM export re-includes EVERY
client ever cleared, including ones from before agent_portal existed that
Commission History already backfilled. Without a guard, the first live CRM
upload after a History backfill would re-credit those same clients a
second time as a brand-new unit in a fresh calculated period for the same
month they were already paid for.

Owner policy (confirmed August 2026): "This file has already been paid.
Don't calculate it again. Only watch it going forward to see if it drops
and needs a clawback." — see commission_core/crm_parser.py's module
docstring (already_history_paid_crm_ids) for the mechanism.
"""

import csv
import io

from agent_portal.models import AgentCommission, ClientRecord, CommissionPeriod
from test_commission_history_period_overlap import (
    _crm_csv_bytes, _history_csv_bytes, _login_as, _make_admin,
)


class TestAlreadyHistoryPaidClientsAreNotRecalculated:
    def test_a_still_active_history_paid_client_is_not_recredited_by_a_later_crm_upload(self, app, db, client):
        admin = _make_admin(db)
        _login_as(client, admin)

        # Commission History says crm_id 777 was already paid for 2026-01.
        history_bytes = _history_csv_bytes([
            {"month": "January", "id": "777", "agent": "Agent A", "name": "Old Client", "debt": "20000"},
        ])
        client.post(
            "/admin/upload-commission-history",
            data={"history_year": "2026", "history_file": (io.BytesIO(history_bytes), "history.csv")},
            content_type="multipart/form-data",
        )

        # A full CRM export re-includes that same still-active client (no drop),
        # alongside a genuinely new client the same agent cleared this month.
        crm_bytes = _crm_csv_bytes([
            {"ID": "777", "Sales Rep": "Agent A", "Full Name": "Old Client",
             "1st Payment Cleared Date": "01/15/2026", "Status": "Active",
             "Enrolled Debt": "20000", "# NSF": "0", "Payments Made": "5", "Pay Freq.": "Monthly"},
            {"ID": "888", "Sales Rep": "Agent A", "Full Name": "New Client",
             "1st Payment Cleared Date": "01/20/2026", "Status": "Active",
             "Enrolled Debt": "9000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly"},
        ])
        resp = client.post(
            "/admin/upload-csv", data={"csv_file": (io.BytesIO(crm_bytes), "crm.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"already paid via a Commission History import" in resp.data

        with app.app_context():
            crm_period = CommissionPeriod.query.filter_by(period_label="2026-01", source="crm").one()
            agent_row = AgentCommission.query.filter_by(period_id=crm_period.id, agent_name="Agent A").one()

            # Only the genuinely new client counted toward this month's tier/debt —
            # crm_id 777's $20,000 never entered this calculation.
            assert agent_row.units_cleared == 1
            assert agent_row.total_cleared_debt == 9000.0

            # No ClientRecord for crm_id 777 was created under the new calculated period.
            crm_ids_in_new_period = {
                c.crm_id for c in ClientRecord.query.filter_by(period_id=crm_period.id)
            }
            assert crm_ids_in_new_period == {"888"}

            # The historical record is untouched — still exactly the one paid client.
            history_period = CommissionPeriod.query.filter_by(
                period_label="2026-01", source="history_import").one()
            assert ClientRecord.query.filter_by(period_id=history_period.id).count() == 1

    def test_a_history_paid_client_who_later_drops_is_still_clawed_back(self, app, db, client):
        """'Only watch it going forward' — the client must still be caught if
        they drop in a LATER upload, with no extra plumbing needed (clawback
        classification never depended on already_history_paid_crm_ids)."""
        admin = _make_admin(db)
        _login_as(client, admin)

        history_bytes = _history_csv_bytes([
            {"month": "January", "id": "777", "agent": "Agent A", "name": "Old Client", "debt": "20000"},
        ])
        client.post(
            "/admin/upload-commission-history",
            data={"history_year": "2026", "history_file": (io.BytesIO(history_bytes), "history.csv")},
            content_type="multipart/form-data",
        )

        # First CRM upload: still active, correctly skipped (see test above).
        crm_bytes_1 = _crm_csv_bytes([{
            "ID": "777", "Sales Rep": "Agent A", "Full Name": "Old Client",
            "1st Payment Cleared Date": "01/15/2026", "Status": "Active",
            "Enrolled Debt": "20000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly",
        }])
        client.post("/admin/upload-csv", data={"csv_file": (io.BytesIO(crm_bytes_1), "crm1.csv")},
                    content_type="multipart/form-data")

        # A second CRM upload later shows they dropped, before hitting the
        # safe payment threshold — must still trigger a real clawback.
        crm_bytes_2 = _crm_csv_bytes([{
            "ID": "777", "Sales Rep": "Agent A", "Full Name": "Old Client",
            "1st Payment Cleared Date": "01/15/2026", "Dropped Date": "06/20/2026",
            "Status": "Cancelled", "Enrolled Debt": "20000", "# NSF": "0",
            "Payments Made": "1", "Pay Freq.": "Monthly",
        }])
        resp = client.post(
            "/admin/upload-csv", data={"csv_file": (io.BytesIO(crm_bytes_2), "crm2.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        assert resp.status_code == 200

        with app.app_context():
            clawback_row = ClientRecord.query.filter_by(crm_id="777", clawback_applied=True).first()
            assert clawback_row is not None
            assert clawback_row.clawback_amount > 0

    def test_a_history_paid_safe_cancel_client_does_not_inflate_a_later_period_s_tier(self, app, db, client):
        """A history-paid client who shows up later as a protected drop
        (safe_cancel — enough payments made before dropping) must not count
        as a fresh unit toward a NEW calculated period's tier either — that
        unit was already credited when Commission History was imported."""
        admin = _make_admin(db)
        _login_as(client, admin)

        history_bytes = _history_csv_bytes([
            {"month": "January", "id": "777", "agent": "Agent A", "name": "Old Client", "debt": "20000"},
        ])
        client.post(
            "/admin/upload-commission-history",
            data={"history_year": "2026", "history_file": (io.BytesIO(history_bytes), "history.csv")},
            content_type="multipart/form-data",
        )

        # Cleared 01/2026, dropped 06/2026, but with enough payments made to
        # be safe-cancel protected — still classifies as a unit, just $0.
        crm_bytes = _crm_csv_bytes([
            {"ID": "777", "Sales Rep": "Agent A", "Full Name": "Old Client",
             "1st Payment Cleared Date": "01/15/2026", "Dropped Date": "06/20/2026",
             "Status": "Cancelled", "Enrolled Debt": "20000", "# NSF": "0",
             "Payments Made": "2", "Pay Freq.": "Monthly"},
            {"ID": "888", "Sales Rep": "Agent A", "Full Name": "New Client",
             "1st Payment Cleared Date": "01/20/2026", "Status": "Active",
             "Enrolled Debt": "9000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly"},
        ])
        resp = client.post(
            "/admin/upload-csv", data={"csv_file": (io.BytesIO(crm_bytes), "crm.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        assert resp.status_code == 200

        with app.app_context():
            crm_period = CommissionPeriod.query.filter_by(period_label="2026-01", source="crm").one()
            agent_row = AgentCommission.query.filter_by(period_id=crm_period.id, agent_name="Agent A").one()
            # Only the genuinely new client — the already-paid safe_cancel
            # client contributes no unit here either.
            assert agent_row.units_cleared == 1
