"""Regression tests for the Commission History "Rate" column (owner-added):
the exact rate a client's original commission was actually paid at, so a
later clawback can use it verbatim (enrolled_debt * paid_rate) instead of
recalculating a rate through the tier table.

Real reported example: AJ Valipour paid 1.40% of $42,869.00 = $600.17 on
Dustin Holte (crm_id 1181065497) in January. When that file's client shows a
Dropped Date in a later CRM upload (e.g. July), the clawback must be exactly
$600.17 — not whatever the tier-recalculation formula would otherwise produce.
"""

import io

import pytest

from agent_portal.models import AgentCommission, ClientRecord, CommissionPeriod
from test_commission_history_period_overlap import (
    _crm_csv_bytes, _login_as, _make_admin,
)


def _history_csv_bytes_with_rate(rows):
    """Like test_commission_history_period_overlap.py's _history_csv_bytes,
    but with the owner-added Rate column."""
    out = "Month,ID,Sales Rep,Full Name,Enrolled Debt,To subtract,Payments Made,Rate\n"
    for r in rows:
        out += (f"{r['month']},{r['id']},{r['agent']},{r.get('name', '')},"
                f"{r.get('debt', '')},{r.get('subtract', '')},{r.get('payments', '')},"
                f"{r.get('rate', '')}\n")
    return out.encode("utf-8")


class TestClawbackUsesHistoryRateVerbatim:
    def test_clawback_amount_is_debt_times_history_rate(self, app, db, client):
        admin = _make_admin(db)
        _login_as(client, admin)

        history_bytes = _history_csv_bytes_with_rate([
            {"month": "January", "id": "1181065497", "agent": "AJ Valipour",
             "name": "Dustin Holte", "debt": "42869", "payments": "2", "rate": "1.40%"},
        ])
        client.post(
            "/admin/upload-commission-history",
            data={"history_year": "2026", "history_file": (io.BytesIO(history_bytes), "history.csv")},
            content_type="multipart/form-data",
        )

        with app.app_context():
            seeded = ClientRecord.query.filter_by(crm_id="1181065497").one()
            assert seeded.paid_rate == pytest.approx(0.014)

        crm_bytes = _crm_csv_bytes([{
            "ID": "1181065497", "Sales Rep": "AJ Valipour", "Full Name": "Dustin Holte",
            "1st Payment Cleared Date": "01/15/2026", "Dropped Date": "07/20/2026",
            "Status": "Cancelled", "Enrolled Debt": "42869", "# NSF": "0",
            "Payments Made": "1", "Pay Freq.": "Monthly",
        }])
        resp = client.post(
            "/admin/upload-csv", data={"csv_file": (io.BytesIO(crm_bytes), "crm.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        assert resp.status_code == 200

        with app.app_context():
            clawback_row = ClientRecord.query.filter_by(
                crm_id="1181065497", clawback_applied=True).first()
            assert clawback_row is not None
            assert clawback_row.clawback_amount == 600.17  # 42,869 x 1.40%

    def test_falls_back_to_tier_recalculation_when_no_rate_is_known(self, app, db, client):
        """A History row with no Rate column value must fall through to the
        ordinary tier-recalculation formula, unaffected."""
        admin = _make_admin(db)
        _login_as(client, admin)

        history_bytes = _history_csv_bytes_with_rate([
            {"month": "January", "id": "1181065497", "agent": "AJ Valipour",
             "name": "Dustin Holte", "debt": "42869", "payments": "2"},  # no rate
        ])
        client.post(
            "/admin/upload-commission-history",
            data={"history_year": "2026", "history_file": (io.BytesIO(history_bytes), "history.csv")},
            content_type="multipart/form-data",
        )

        with app.app_context():
            seeded = ClientRecord.query.filter_by(crm_id="1181065497").one()
            assert seeded.paid_rate is None

        crm_bytes = _crm_csv_bytes([{
            "ID": "1181065497", "Sales Rep": "AJ Valipour", "Full Name": "Dustin Holte",
            "1st Payment Cleared Date": "01/15/2026", "Dropped Date": "07/20/2026",
            "Status": "Cancelled", "Enrolled Debt": "42869", "# NSF": "0",
            "Payments Made": "1", "Pay Freq.": "Monthly",
        }])
        client.post(
            "/admin/upload-csv", data={"csv_file": (io.BytesIO(crm_bytes), "crm.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )

        with app.app_context():
            clawback_row = ClientRecord.query.filter_by(
                crm_id="1181065497", clawback_applied=True).first()
            assert clawback_row is not None
            # 1 unit, Tier 1 (1%) -> 428.69, arrived at via the ordinary
            # tier-recalculation "only unit that month" shortcut, NOT a Rate
            # override (there was none for this crm_id).
            assert clawback_row.clawback_amount == 428.69
