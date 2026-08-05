"""End-to-end coverage (real /upload-crm POSTs, not just the parser in
isolation) for the clawback-placement policy change (owner-confirmed August
2026, applies to app/ too): a clawback now lands in the client's own Dropped
Date month instead of "latest period in file." That change made the "period
already exists" skip-guard a much bigger deal than it used to be — a
client's own Dropped Date is a fixed, already-imported month far more often
than "latest period in file" ever was — so this specifically exercises the
two mechanisms that change required:

1. A clawback whose target month already has a saved calculated period must
   still be applied (via find-or-create, mirroring how Cordoba chargebacks
   have always attached a deduction to an existing period) — not silently
   dropped the way a genuinely duplicate re-import correctly still is.
2. Re-uploading a file that still contains an already-clawed-back client's
   row (which every full-history CRM export always will, forever) must
   never re-apply that same clawback a second time.
"""

import csv
import io

from app.models import AgentCommission, ClientRecord, CommissionPeriod

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


class TestClawbackAppliesToAnAlreadyExistingPeriod:
    def test_clawback_lands_in_existing_period_via_find_or_create(self, app, db, client):
        first = _crm_csv_bytes([
            {"ID": "1", "Sales Rep": "Agent A", "Full Name": "Drops Later",
             "1st Payment Cleared Date": "06/10/2026", "Status": "Active",
             "Enrolled Debt": "10000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly"},
            {"ID": "2", "Sales Rep": "Agent B", "Full Name": "August Regular",
             "1st Payment Cleared Date": "08/05/2026", "Status": "Active",
             "Enrolled Debt": "9000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly"},
        ])
        client.post("/upload-crm", data={"csv_file": (io.BytesIO(first), "f1.csv")},
                    content_type="multipart/form-data")

        with app.app_context():
            august_period = CommissionPeriod.query.filter_by(period_label="2026-08").one()
            agent_b_row = AgentCommission.query.filter_by(
                period_id=august_period.id, agent_name="Agent B").one()
            assert agent_b_row.units_cleared == 1

        second = _crm_csv_bytes([
            {"ID": "1", "Sales Rep": "Agent A", "Full Name": "Drops Later",
             "1st Payment Cleared Date": "06/10/2026", "Dropped Date": "08/12/2026",
             "Status": "Cancelled", "Enrolled Debt": "10000", "# NSF": "0",
             "Payments Made": "1", "Pay Freq.": "Monthly"},
        ])
        resp = client.post(
            "/upload-crm", data={"csv_file": (io.BytesIO(second), "f2.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"already existed" in resp.data
        assert b"applied 1 new" in resp.data

        with app.app_context():
            agent_b_row = AgentCommission.query.filter_by(
                period_id=august_period.id, agent_name="Agent B").one()
            assert agent_b_row.units_cleared == 1
            assert agent_b_row.clawback_amount == 0.0

            agent_a_row = AgentCommission.query.filter_by(
                period_id=august_period.id, agent_name="Agent A").one()
            assert agent_a_row.units_cleared == 0
            assert agent_a_row.clawback_amount == 100.0  # 10,000 x 1% fallback

            clawback_client = ClientRecord.query.filter_by(
                crm_id="1", clawback_applied=True).one()
            assert clawback_client.period_id == august_period.id
            assert clawback_client.agent_commission_id == agent_a_row.id

    def test_reuploading_the_same_file_never_double_claws_back(self, app, db, client):
        # app/'s policy (require_prior_payment_evidence=True, unchanged by
        # this session's work) needs proof of payment before a clawback
        # applies — so first clear the client for real (upload 1)...
        cleared = _crm_csv_bytes([
            {"ID": "1", "Sales Rep": "Agent A", "Full Name": "Drops Later",
             "1st Payment Cleared Date": "06/10/2026", "Status": "Active",
             "Enrolled Debt": "10000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly"},
        ])
        client.post("/upload-crm", data={"csv_file": (io.BytesIO(cleared), "f1.csv")},
                    content_type="multipart/form-data")

        # ...then a second upload reflects the drop (now provable via
        # already_cleared_crm_ids, since upload 1 saved is_cleared=True).
        dropped = _crm_csv_bytes([
            {"ID": "1", "Sales Rep": "Agent A", "Full Name": "Drops Later",
             "1st Payment Cleared Date": "06/10/2026", "Dropped Date": "08/12/2026",
             "Status": "Cancelled", "Enrolled Debt": "10000", "# NSF": "0",
             "Payments Made": "1", "Pay Freq.": "Monthly"},
        ])
        client.post("/upload-crm", data={"csv_file": (io.BytesIO(dropped), "f2.csv")},
                    content_type="multipart/form-data")

        with app.app_context():
            assert ClientRecord.query.filter_by(crm_id="1", clawback_applied=True).count() == 1
            total_after_first = sum(
                a.clawback_amount for a in AgentCommission.query.filter_by(agent_name="Agent A").all()
            )
            assert total_after_first == 100.0

        # A full-history export still contains crm_id "1" (cleared+dropped,
        # unchanged) forever — re-uploading that same reflects-the-drop file
        # again must be a complete no-op, not a second deduction.
        resp = client.post(
            "/upload-crm", data={"csv_file": (io.BytesIO(dropped), "f2.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        assert resp.status_code == 200

        with app.app_context():
            assert ClientRecord.query.filter_by(crm_id="1", clawback_applied=True).count() == 1
            total_after_second = sum(
                a.clawback_amount for a in AgentCommission.query.filter_by(agent_name="Agent A").all()
            )
            assert total_after_second == 100.0

    def test_genuine_new_units_for_an_existing_period_still_blocked_while_clawback_still_applies(
        self, app, db, client,
    ):
        first = _crm_csv_bytes([
            {"ID": "1", "Sales Rep": "Agent A", "Full Name": "Drops Later",
             "1st Payment Cleared Date": "06/10/2026", "Status": "Active",
             "Enrolled Debt": "10000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly"},
            {"ID": "2", "Sales Rep": "Agent B", "Full Name": "August Regular",
             "1st Payment Cleared Date": "08/05/2026", "Status": "Active",
             "Enrolled Debt": "9000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly"},
        ])
        client.post("/upload-crm", data={"csv_file": (io.BytesIO(first), "f1.csv")},
                    content_type="multipart/form-data")

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
            "/upload-crm", data={"csv_file": (io.BytesIO(second), "f2.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"already exists" in resp.data  # the genuine-units warning

        with app.app_context():
            august_period = CommissionPeriod.query.filter_by(period_label="2026-08").one()
            agent_b_row = AgentCommission.query.filter_by(
                period_id=august_period.id, agent_name="Agent B").one()
            assert agent_b_row.units_cleared == 1
            assert ClientRecord.query.filter_by(crm_id="3").count() == 0

            agent_a_row = AgentCommission.query.filter_by(
                period_id=august_period.id, agent_name="Agent A").one()
            assert agent_a_row.clawback_amount == 100.0
