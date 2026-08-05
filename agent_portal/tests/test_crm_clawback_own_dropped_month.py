"""End-to-end coverage (real /admin/upload-csv POSTs, not just the parser in
isolation) for the clawback-placement policy change (owner-confirmed August
2026): a clawback now lands in the client's own Dropped Date month instead
of "latest period in file." That change made the "period already exists"
skip-guard a much bigger deal than it used to be — a client's own Dropped
Date is a fixed, already-imported month far more often than "latest period
in file" ever was — so this specifically exercises the two mechanisms that
change required:

1. A clawback whose target month already has a saved calculated period must
   still be applied (via find-or-create, mirroring how Cordoba chargebacks
   have always attached a deduction to an existing period) — not silently
   dropped the way a genuinely duplicate re-import correctly still is.
2. Re-uploading a file that still contains an already-clawed-back client's
   row (which every full-history CRM export always will, forever) must
   never re-apply that same clawback a second time — the target month no
   longer advances forward the way "latest period in file" used to, so
   without this guard every future upload would re-clawback the same client.
"""

import io

from agent_portal.models import AgentCommission, ClientRecord, CommissionPeriod
from test_commission_history_period_overlap import _crm_csv_bytes, _login_as, _make_admin


class TestClawbackAppliesToAnAlreadyExistingPeriod:
    def test_clawback_lands_in_existing_period_via_find_or_create(self, app, db, client):
        admin = _make_admin(db)
        _login_as(client, admin)

        # Upload 1: client clears in June (own upload) AND some unrelated
        # activity creates a real, saved period for August — the eventual
        # Dropped Date month — before the clawback is ever discovered.
        first = _crm_csv_bytes([
            {"ID": "1", "Sales Rep": "Agent A", "Full Name": "Drops Later",
             "1st Payment Cleared Date": "06/10/2026", "Status": "Active",
             "Enrolled Debt": "10000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly"},
            {"ID": "2", "Sales Rep": "Agent B", "Full Name": "August Regular",
             "1st Payment Cleared Date": "08/05/2026", "Status": "Active",
             "Enrolled Debt": "9000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly"},
        ])
        client.post("/admin/upload-csv", data={"csv_file": (io.BytesIO(first), "f1.csv")},
                    content_type="multipart/form-data")

        with app.app_context():
            august_period = CommissionPeriod.query.filter_by(period_label="2026-08", source="crm").one()
            agent_b_row = AgentCommission.query.filter_by(
                period_id=august_period.id, agent_name="Agent B").one()
            assert agent_b_row.units_cleared == 1  # unaffected baseline

        # Upload 2: crm_id "1" now shows a Dropped Date in August — the SAME
        # month that already has a saved calculated period from upload 1.
        second = _crm_csv_bytes([
            {"ID": "1", "Sales Rep": "Agent A", "Full Name": "Drops Later",
             "1st Payment Cleared Date": "06/10/2026", "Dropped Date": "08/12/2026",
             "Status": "Cancelled", "Enrolled Debt": "10000", "# NSF": "0",
             "Payments Made": "1", "Pay Freq.": "Monthly"},
        ])
        resp = client.post(
            "/admin/upload-csv", data={"csv_file": (io.BytesIO(second), "f2.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"already existed" in resp.data
        assert b"applied 1 new" in resp.data

        with app.app_context():
            # August's existing period is untouched for Agent B...
            agent_b_row = AgentCommission.query.filter_by(
                period_id=august_period.id, agent_name="Agent B").one()
            assert agent_b_row.units_cleared == 1
            assert agent_b_row.clawback_amount == 0.0

            # ...but Agent A now has a new zero-unit holding row IN THAT SAME
            # existing period, carrying the clawback — not lost, not a
            # separate never-visible period.
            agent_a_row = AgentCommission.query.filter_by(
                period_id=august_period.id, agent_name="Agent A").one()
            assert agent_a_row.units_cleared == 0
            assert agent_a_row.clawback_amount == 100.0  # 10,000 x 1% fallback

            clawback_client = ClientRecord.query.filter_by(
                crm_id="1", clawback_applied=True).one()
            assert clawback_client.period_id == august_period.id
            assert clawback_client.agent_commission_id == agent_a_row.id

    def test_reuploading_the_same_file_never_double_claws_back(self, app, db, client):
        """A full-history CRM export always re-includes every client ever
        seen, including already-clawed-back ones — re-uploading it must be a
        complete no-op for that client, not a repeat deduction."""
        admin = _make_admin(db)
        _login_as(client, admin)

        data = _crm_csv_bytes([
            {"ID": "1", "Sales Rep": "Agent A", "Full Name": "Drops Later",
             "1st Payment Cleared Date": "06/10/2026", "Dropped Date": "08/12/2026",
             "Status": "Cancelled", "Enrolled Debt": "10000", "# NSF": "0",
             "Payments Made": "1", "Pay Freq.": "Monthly"},
        ])
        client.post("/admin/upload-csv", data={"csv_file": (io.BytesIO(data), "f1.csv")},
                    content_type="multipart/form-data")

        with app.app_context():
            assert ClientRecord.query.filter_by(crm_id="1", clawback_applied=True).count() == 1
            total_after_first = sum(
                a.clawback_amount for a in AgentCommission.query.filter_by(agent_name="Agent A").all()
            )
            assert total_after_first == 100.0

        # Re-upload the EXACT same file again (a routine daily re-sync, say).
        resp = client.post(
            "/admin/upload-csv", data={"csv_file": (io.BytesIO(data), "f1.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        assert resp.status_code == 200

        with app.app_context():
            # Still exactly one clawback ClientRecord for this crm_id, and the
            # total clawed back from Agent A hasn't grown.
            assert ClientRecord.query.filter_by(crm_id="1", clawback_applied=True).count() == 1
            total_after_second = sum(
                a.clawback_amount for a in AgentCommission.query.filter_by(agent_name="Agent A").all()
            )
            assert total_after_second == 100.0

    def test_genuine_new_units_for_an_existing_period_still_blocked_while_clawback_still_applies(
        self, app, db, client,
    ):
        """The existing double-import protection for genuine calculated data
        must survive this change untouched: if a re-upload tries to credit
        NEW units for a month that already has a saved period, those units
        are still skipped (with a warning) — only a co-occurring clawback for
        that same month is new behavior."""
        admin = _make_admin(db)
        _login_as(client, admin)

        first = _crm_csv_bytes([
            {"ID": "1", "Sales Rep": "Agent A", "Full Name": "Drops Later",
             "1st Payment Cleared Date": "06/10/2026", "Status": "Active",
             "Enrolled Debt": "10000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly"},
            {"ID": "2", "Sales Rep": "Agent B", "Full Name": "August Regular",
             "1st Payment Cleared Date": "08/05/2026", "Status": "Active",
             "Enrolled Debt": "9000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly"},
        ])
        client.post("/admin/upload-csv", data={"csv_file": (io.BytesIO(first), "f1.csv")},
                    content_type="multipart/form-data")

        # A second upload tries to ALSO credit a brand-new August client for
        # Agent B (would double-count if applied) AND reflects crm_id "1"'s
        # drop in that same August month.
        second = _crm_csv_bytes([
            {"ID": "1", "Sales Rep": "Agent A", "Full Name": "Drops Later",
             "1st Payment Cleared Date": "06/10/2026", "Dropped Date": "08/12/2026",
             "Status": "Cancelled", "Enrolled Debt": "10000", "# NSF": "0",
             "Payments Made": "1", "Pay Freq.": "Monthly"},
            {"ID": "3", "Sales Rep": "Agent B", "Full Name": "Should Not Import",
             "1st Payment Cleared Date": "08/06/2026", "Status": "Active",
             "Enrolled Debt": "50000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly"},
        ])
        resp = client.post(
            "/admin/upload-csv", data={"csv_file": (io.BytesIO(second), "f2.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"already exists" in resp.data  # the genuine-units warning

        with app.app_context():
            august_period = CommissionPeriod.query.filter_by(period_label="2026-08", source="crm").one()
            # Agent B's genuine unit count is untouched — crm_id "3" was NOT imported.
            agent_b_row = AgentCommission.query.filter_by(
                period_id=august_period.id, agent_name="Agent B").one()
            assert agent_b_row.units_cleared == 1
            assert ClientRecord.query.filter_by(crm_id="3").count() == 0

            # But the clawback for crm_id "1" still applied.
            agent_a_row = AgentCommission.query.filter_by(
                period_id=august_period.id, agent_name="Agent A").one()
            assert agent_a_row.clawback_amount == 100.0
