"""Regression tests for the Commission History "Rate" column (owner-added):
the exact rate a client's original commission was actually paid at, so a
later clawback can use it verbatim (enrolled_debt * paid_rate) instead of
recalculating a rate through the tier table.

Real reported example: AJ Valipour paid 1.40% of $42,869.00 = $600.17 on
Dustin Holte (crm_id 1181065497) in January. When that file's client shows a
Dropped Date in a later CRM upload (e.g. July), the clawback must be exactly
$600.17 — not whatever the tier-recalculation formula would otherwise produce.
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


def _seed_history_paid_client(db, crm_id, agent_name, enrolled_debt, paid_rate,
                               period_label="2026-01"):
    """Mirrors what a Commission History import with a Rate column would have
    saved: a real paid ClientRecord, is_cleared=True, with paid_rate set."""
    period = CommissionPeriod.query.filter_by(period_label=period_label).first()
    if not period:
        period = CommissionPeriod(period_label=period_label, filename="history.xlsx", total_agents=1)
        db.session.add(period)
        db.session.flush()
    agent = AgentCommission.query.filter_by(period_id=period.id, agent_name=agent_name).first()
    if not agent:
        # AgentCommission's own tier_rate/gross_commission still needs a real
        # number even when paid_rate is None (testing the "no known rate on
        # this client" fallback path) — Tier 1's 1% stands in for whatever the
        # tier-recalculation formula would have used in that case.
        agent_rate = paid_rate if paid_rate is not None else 0.01
        gross = round(enrolled_debt * agent_rate, 2)
        agent = AgentCommission(
            period_id=period.id, agent_name=agent_name,
            units_cleared=1, total_cleared_debt=enrolled_debt, cancellation_rate=0.0,
            hourly_draw=0.0, raw_tier=1, adjusted_tier=1, tier_rate=agent_rate,
            gross_commission=gross, clawback_amount=0.0,
            net_commission=gross, payout=gross,
            payout_type="commission", source="history_import", notes="",
        )
        db.session.add(agent)
        db.session.flush()
    db.session.add(ClientRecord(
        period_id=period.id, agent_commission_id=agent.id,
        crm_id=crm_id, agent_name=agent_name, client_name="Dustin Holte",
        enrolled_debt=enrolled_debt, paid_rate=paid_rate, is_cleared=True,
        first_payment_cleared_date="2026-01-15", payments_made=2, pay_freq="Monthly",
    ))
    db.session.commit()
    return period, agent


class TestClawbackUsesHistoryRateVerbatim:
    def test_clawback_amount_is_debt_times_history_rate(self, app, db, client):
        _seed_history_paid_client(
            db, crm_id="1181065497", agent_name="AJ Valipour",
            enrolled_debt=42_869.0, paid_rate=0.014,
        )

        crm_bytes = _crm_csv_bytes([{
            "ID": "1181065497", "Sales Rep": "AJ Valipour", "Full Name": "Dustin Holte",
            "1st Payment Cleared Date": "2026-01-15", "Dropped Date": "07/20/2026",
            "Status": "Cancelled", "Enrolled Debt": "42869", "# NSF": "0",
            "Payments Made": "1", "Pay Freq.": "Monthly",
        }])
        resp = client.post(
            "/upload-crm", data={"csv_file": (io.BytesIO(crm_bytes), "crm.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        assert resp.status_code == 200

        with app.app_context():
            clawback_row = ClientRecord.query.filter_by(
                crm_id="1181065497", clawback_applied=True).first()
            assert clawback_row is not None
            assert clawback_row.clawback_amount == 600.17  # 42,869 x 1.40%

    def test_rate_overrides_what_tier_recalculation_would_otherwise_produce(self, app, db, client):
        """Prove this is a real bypass, not a coincidence: seed a SECOND
        client for the same agent+month so a tier-recalculation WOULD produce
        a different number, and confirm the known rate still wins."""
        _seed_history_paid_client(
            db, crm_id="1181065497", agent_name="AJ Valipour",
            enrolled_debt=42_869.0, paid_rate=0.014,
        )
        with app.app_context():
            period = CommissionPeriod.query.filter_by(period_label="2026-01").one()
            agent = AgentCommission.query.filter_by(period_id=period.id, agent_name="AJ Valipour").one()
            # Bump the agent's recorded tier/debt so a tier-recalculation-based
            # clawback would land on a materially different number (2% instead
            # of the recorded 1.40%) if the Rate override weren't taking effect.
            agent.total_cleared_debt = 42_869.0
            agent.gross_commission = round(42_869.0 * 0.02, 2)
            agent.tier_rate = 0.02
            db.session.commit()

        crm_bytes = _crm_csv_bytes([{
            "ID": "1181065497", "Sales Rep": "AJ Valipour", "Full Name": "Dustin Holte",
            "1st Payment Cleared Date": "2026-01-15", "Dropped Date": "07/20/2026",
            "Status": "Cancelled", "Enrolled Debt": "42869", "# NSF": "0",
            "Payments Made": "1", "Pay Freq.": "Monthly",
        }])
        client.post(
            "/upload-crm", data={"csv_file": (io.BytesIO(crm_bytes), "crm.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )

        with app.app_context():
            clawback_row = ClientRecord.query.filter_by(
                crm_id="1181065497", clawback_applied=True).first()
            assert clawback_row is not None
            assert clawback_row.clawback_amount == 600.17  # NOT 857.38 (42,869 x 2%)

    def test_falls_back_to_tier_recalculation_when_no_rate_is_known(self, app, db, client):
        """A client whose original record has no paid_rate (e.g. History file
        predates the Rate column, or the client was actually paid via a live
        CRM-computed period, which has no Rate column at all) must fall
        through to the ordinary tier-recalculation formula, unaffected."""
        _seed_history_paid_client(
            db, crm_id="1181065497", agent_name="AJ Valipour",
            enrolled_debt=42_869.0, paid_rate=None,
        )

        crm_bytes = _crm_csv_bytes([{
            "ID": "1181065497", "Sales Rep": "AJ Valipour", "Full Name": "Dustin Holte",
            "1st Payment Cleared Date": "2026-01-15", "Dropped Date": "07/20/2026",
            "Status": "Cancelled", "Enrolled Debt": "42869", "# NSF": "0",
            "Payments Made": "1", "Pay Freq.": "Monthly",
        }])
        client.post(
            "/upload-crm", data={"csv_file": (io.BytesIO(crm_bytes), "crm.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )

        with app.app_context():
            clawback_row = ClientRecord.query.filter_by(
                crm_id="1181065497", clawback_applied=True).first()
            assert clawback_row is not None
            # No known Rate for this crm_id -> falls through to the ordinary
            # tier-recalculation path. 1 unit -> the "only unit that month"
            # shortcut returns the whole month's recorded gross_commission
            # (42,869 x 1% Tier 1 = 428.69, the fixture's fallback agent rate
            # for this no-known-rate case) — proves the Rate override is
            # opt-in per crm_id, not a blanket change to clawback math.
            assert clawback_row.clawback_amount == 428.69
