"""Regression test for a real production bug found via a live case: 9+ clawbacks
across 9 different agents all landed at $0.00 on agent_portal's "Clawback Files"
admin view.

Root cause: parse_crm_and_calculate()'s Step 3 needs to know what an agent's
tier/commission actually was in their original cleared month, to compute how
much of it to claw back on a later drop. It reconstructs that by re-running
Steps 1-2 on the SAME uploaded file's own rows. That's accurate for a month
that's entirely CRM-computed — but agent_portal also allows a month to be
mostly (or entirely) backfilled via a separate Commission History import, and
already_history_paid_crm_ids deliberately excludes those already-paid clients
from the in-file recomputation (to avoid double-crediting them). If only one
non-history-paid client is left over for that agent+month — and that leftover
happens to be a Credit Score <= 500 client, who counts as a unit but
contributes $0 debt/commission — the recomputed "original period" ends up
with units_cleared=1 and gross_commission=$0.0, and
calculate_clawback_amount's "orig_units <= 1 -> claw back the whole month's
gross_commission" shortcut returns that unrelated $0 for a COMPLETELY
DIFFERENT (real, full-commission) client's clawback.

Fix: known_period_totals() (ingest.py) gives Step 3 the DB's actual saved
totals for that (agent, period_label) — summed across every CommissionPeriod
source sharing that label — to use INSTEAD OF the in-file recomputation
whenever a DB record already exists. See commission_core/crm_parser.py's
module docstring item 4.
"""

import csv
import io

from agent_portal.models import AgentCommission, ClientRecord, CommissionPeriod
from test_commission_history_period_overlap import _login_as, _make_admin

CSV_HEADERS = [
    "ID", "Sales Rep", "Full Name", "1st Payment Cleared Date", "Dropped Date",
    "Status", "Enrolled Debt", "# NSF", "Payments Made", "Pay Freq.", "Credit Score",
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


class TestClawbackUsesRealOriginalPeriodTotalsNotAnIncompleteRecompute:
    def test_history_paid_client_dropping_is_not_clawed_back_at_zero(self, app, db, client):
        admin = _make_admin(db)
        _login_as(client, admin)

        # Commission History backfills January for Agent A: 20 real clients,
        # $500,000 total debt -> Tier 6 (61+... actually 20 units is Tier 1,
        # keep it simple and unambiguous: one big paid client is enough to
        # prove the point without needing an exact tier boundary).
        history_bytes = _history_csv_bytes([
            {"month": "January", "id": "777", "agent": "Agent A",
             "name": "Real Paid Client", "debt": "20000", "payments": "5"},
        ])
        client.post(
            "/admin/upload-commission-history",
            data={"history_year": "2026", "history_file": (io.BytesIO(history_bytes), "history.csv")},
            content_type="multipart/form-data",
        )

        # First CRM upload: the history-paid client still active (correctly
        # skipped from recrediting — see test_commission_history_no_double_pay.py),
        # PLUS a genuinely new, unrelated Credit Score <= 500 client who is NOT
        # in the history file at all — this is the "leftover" that used to
        # corrupt Step 3's reconstruction of Agent A's January.
        crm_bytes_1 = _crm_csv_bytes([
            {"ID": "777", "Sales Rep": "Agent A", "Full Name": "Real Paid Client",
             "1st Payment Cleared Date": "01/15/2026", "Status": "Active",
             "Enrolled Debt": "20000", "# NSF": "0", "Payments Made": "5", "Pay Freq.": "Monthly"},
            {"ID": "888", "Sales Rep": "Agent A", "Full Name": "Low Credit Client",
             "1st Payment Cleared Date": "01/20/2026", "Status": "Active",
             "Enrolled Debt": "9000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly",
             "Credit Score": "450"},
        ])
        client.post("/admin/upload-csv", data={"csv_file": (io.BytesIO(crm_bytes_1), "crm1.csv")},
                    content_type="multipart/form-data")

        with app.app_context():
            crm_period = CommissionPeriod.query.filter_by(period_label="2026-01", source="crm").one()
            agent_row = AgentCommission.query.filter_by(period_id=crm_period.id, agent_name="Agent A").one()
            # Sanity check on the bug's precondition: the "crm" period for this
            # month really is down to 1 unit / $0 debt (the low-credit leftover)
            # once the history-paid client is excluded.
            assert agent_row.units_cleared == 1
            assert agent_row.total_cleared_debt == 0.0
            assert agent_row.gross_commission == 0.0

        # A second CRM upload later shows the REAL paid client (777) dropping,
        # before hitting the safe payment threshold.
        crm_bytes_2 = _crm_csv_bytes([{
            "ID": "777", "Sales Rep": "Agent A", "Full Name": "Real Paid Client",
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
            # Tier 1 (1-20 units) is 1% -> the history-imported period's own
            # gross_commission was $200.00 (20,000 x 1%), and it was this
            # agent's ONLY real unit that month, so the whole thing is clawed
            # back. Must NOT be $0.00 (the bug) or a stray recompute artifact.
            assert clawback_row.clawback_amount == 200.0

    def test_known_period_totals_sums_crm_and_history_sources_for_the_same_month(self, app, db, client):
        """A month can have BOTH a small leftover "crm" period AND a real
        "history_import" period for the same agent — known_period_totals()
        must combine them, not just prefer one over the other."""
        admin = _make_admin(db)
        _login_as(client, admin)

        history_bytes = _history_csv_bytes([
            {"month": "March", "id": "111", "agent": "Agent B",
             "name": "History Client", "debt": "10000", "payments": "2"},
        ])
        client.post(
            "/admin/upload-commission-history",
            data={"history_year": "2026", "history_file": (io.BytesIO(history_bytes), "history.csv")},
            content_type="multipart/form-data",
        )

        # A genuinely new client the history file never saw, cleared the same
        # month, creates a real (non-$0) "crm" period alongside it.
        crm_bytes = _crm_csv_bytes([{
            "ID": "222", "Sales Rep": "Agent B", "Full Name": "New Client",
            "1st Payment Cleared Date": "03/10/2026", "Status": "Active",
            "Enrolled Debt": "5000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly",
        }])
        client.post("/admin/upload-csv", data={"csv_file": (io.BytesIO(crm_bytes), "crm.csv")},
                    content_type="multipart/form-data")

        with app.app_context():
            from agent_portal.ingest import known_period_totals
            from commission_core.calculator import agent_identity_key
            totals = known_period_totals()
            combined = totals[(agent_identity_key("Agent B"), "2026-03")]
            assert combined["units_cleared"] == 2
            assert combined["total_cleared_debt"] == 15000.0
            # $100 (history: 10,000 x 1%, Tier 1) + $50 (crm: 5,000 x 1%, Tier 1)
            assert combined["gross_commission"] == 150.0


class TestAgentNameCasingIsCollapsedToOneIdentity:
    """Real production case: the same rep appeared as both "amir moayeri" and
    "Amir Moayeri" — sometimes within the SAME CRM file. Without collapsing
    these to one identity, the tier/commission math itself (not just
    clawbacks) silently splits one agent's production into two smaller,
    wrong calculations."""

    def test_two_casings_in_the_same_crm_file_merge_into_one_agent(self, app, db, client):
        admin = _make_admin(db)
        _login_as(client, admin)

        crm_bytes = _crm_csv_bytes([
            {"ID": "1", "Sales Rep": "amir moayeri", "Full Name": "Client One",
             "1st Payment Cleared Date": "03/05/2026", "Status": "Active",
             "Enrolled Debt": "10000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly"},
            {"ID": "2", "Sales Rep": "Amir Moayeri", "Full Name": "Client Two",
             "1st Payment Cleared Date": "03/07/2026", "Status": "Active",
             "Enrolled Debt": "12000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly"},
        ])
        client.post("/admin/upload-csv", data={"csv_file": (io.BytesIO(crm_bytes), "crm.csv")},
                    content_type="multipart/form-data")

        with app.app_context():
            period = CommissionPeriod.query.filter_by(period_label="2026-03", source="crm").one()
            rows = AgentCommission.query.filter_by(period_id=period.id).all()
            # One merged agent row, not two — both clients' debt combined.
            assert len(rows) == 1
            assert rows[0].units_cleared == 2
            assert rows[0].total_cleared_debt == 22000.0

    def test_history_and_crm_casings_still_match_for_clawback_purposes(self, app, db, client):
        """Same fix, cross-file this time: History says "Amir Moayeri",
        a later CRM upload says "amir moayeri" for the exact same real
        person — known_period_totals() must still find the real total."""
        admin = _make_admin(db)
        _login_as(client, admin)

        history_bytes = _history_csv_bytes([
            {"month": "January", "id": "777", "agent": "Amir Moayeri",
             "name": "Real Paid Client", "debt": "20000", "payments": "5"},
        ])
        client.post(
            "/admin/upload-commission-history",
            data={"history_year": "2026", "history_file": (io.BytesIO(history_bytes), "history.csv")},
            content_type="multipart/form-data",
        )

        # Lowercase spelling this time, plus an unrelated low-credit leftover
        # -- exactly the shape that used to zero this clawback out.
        crm_bytes_1 = _crm_csv_bytes([
            {"ID": "777", "Sales Rep": "amir moayeri", "Full Name": "Real Paid Client",
             "1st Payment Cleared Date": "01/15/2026", "Status": "Active",
             "Enrolled Debt": "20000", "# NSF": "0", "Payments Made": "5", "Pay Freq.": "Monthly"},
            {"ID": "888", "Sales Rep": "amir moayeri", "Full Name": "Low Credit Client",
             "1st Payment Cleared Date": "01/20/2026", "Status": "Active",
             "Enrolled Debt": "9000", "# NSF": "0", "Payments Made": "1", "Pay Freq.": "Monthly",
             "Credit Score": "450"},
        ])
        client.post("/admin/upload-csv", data={"csv_file": (io.BytesIO(crm_bytes_1), "crm1.csv")},
                    content_type="multipart/form-data")

        crm_bytes_2 = _crm_csv_bytes([{
            "ID": "777", "Sales Rep": "amir moayeri", "Full Name": "Real Paid Client",
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
            assert clawback_row.clawback_amount == 200.0  # NOT $0.00

