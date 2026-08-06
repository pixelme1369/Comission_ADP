"""Regression test for a real reported case: a client's Enrolled Debt in a
CRM export can genuinely differ from what was recorded when their commission
was ORIGINALLY calculated (a Commission History import, or an earlier CRM
period) — Cordoba's own systems evidently revise this figure over time.
Confirmed real numbers: Commission History recorded Enrolled Debt $30,688 for
a client; a later CRM export's own row for the same crm_id showed $2,664.62.

Owner policy: a clawback must be based on the ORIGINAL Enrolled Debt (what
commission was actually calculated on), never on whatever a later CRM
re-export happens to show for that crm_id today. See
known_enrolled_debt_by_crm_id's docstring on parse_crm_and_calculate.
"""

import io

from agent_portal.models import ClientRecord
from test_commission_history_period_overlap import (
    _crm_csv_bytes, _history_csv_bytes, _login_as, _make_admin,
)


class TestClawbackUsesOriginallyRecordedEnrolledDebt:
    def test_clawback_amount_and_display_use_history_debt_not_the_later_crm_row(self, app, db, client):
        admin = _make_admin(db)
        _login_as(client, admin)

        # Commission History: crm_id 1208754105 paid on Enrolled Debt $30,688.
        history_bytes = _history_csv_bytes([
            {"month": "January", "id": "1208754105", "agent": "Adam Elqaza",
             "name": "Katherine Kuschtsch", "debt": "30688", "payments": "2"},
        ])
        client.post(
            "/admin/upload-commission-history",
            data={"history_year": "2026", "history_file": (io.BytesIO(history_bytes), "history.csv")},
            content_type="multipart/form-data",
        )

        # A later CRM export shows this same crm_id dropping -- with a
        # DIFFERENT (lower) Enrolled Debt than what was actually paid on.
        crm_bytes = _crm_csv_bytes([{
            "ID": "1208754105", "Sales Rep": "Adam Elqaza", "Full Name": "Katherine Kuschtsch",
            "1st Payment Cleared Date": "01/15/2026", "Dropped Date": "07/30/2026",
            "Status": "Cancelled", "Enrolled Debt": "2664.62", "# NSF": "1",
            "Payments Made": "1", "Pay Freq.": "Monthly",
        }])
        resp = client.post(
            "/admin/upload-csv", data={"csv_file": (io.BytesIO(crm_bytes), "crm.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        assert resp.status_code == 200

        with app.app_context():
            clawback_row = ClientRecord.query.filter_by(
                crm_id="1208754105", clawback_applied=True).first()
            assert clawback_row is not None
            # Displayed Enrolled Debt matches what History actually paid on
            # ($30,688), not the later CRM row's $2,664.62.
            assert clawback_row.enrolled_debt == 30_688.0
            # Clawback math used $30,688 too: Tier 1 (1%) of 30,688 = $306.88,
            # not 1% of $2,664.62 ($26.65).
            assert clawback_row.clawback_amount == 306.88

    def test_no_prior_evidence_means_no_clawback_at_all(self, app, db, client):
        """OWNER POLICY (revised August 2026, real case: Alonzo Caudill / ID
        1223452256): a client whose very first appearance in our data is a
        single CRM row already showing BOTH a cleared and a dropped date has
        no independent proof the agent was ever actually paid on them —
        agent_portal no longer trusts that one row alone (see
        require_clawback_payment_evidence on parse_crm_and_calculate). Before
        this policy, this exact shape used the row's own Enrolled Debt as a
        fallback and clawed back anyway; now there's nothing to claw back at
        all -- reclassified same_month_cancel, same as app/ has always done."""
        admin = _make_admin(db)
        _login_as(client, admin)

        crm_bytes = _crm_csv_bytes([{
            "ID": "999", "Sales Rep": "Agent A", "Full Name": "Same File Client",
            "1st Payment Cleared Date": "01/10/2026", "Dropped Date": "07/30/2026",
            "Status": "Cancelled", "Enrolled Debt": "5000", "# NSF": "0",
            "Payments Made": "1", "Pay Freq.": "Monthly",
        }])
        resp = client.post(
            "/admin/upload-csv", data={"csv_file": (io.BytesIO(crm_bytes), "crm.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        assert resp.status_code == 200

        with app.app_context():
            assert ClientRecord.query.filter_by(crm_id="999", clawback_applied=True).first() is None
