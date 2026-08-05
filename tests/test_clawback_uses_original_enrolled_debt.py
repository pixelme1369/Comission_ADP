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


def _seed_originally_cleared_client(db, crm_id, agent_name, enrolled_debt, period_label="2026-01"):
    """Mirrors what a Commission History import (or an earlier CRM upload)
    would have already saved: a real paid ClientRecord, is_cleared=True, at
    whatever Enrolled Debt commission was actually calculated on."""
    period = CommissionPeriod.query.filter_by(period_label=period_label).first()
    if not period:
        period = CommissionPeriod(period_label=period_label, filename="history.xlsx", total_agents=1)
        db.session.add(period)
        db.session.flush()
    agent = AgentCommission.query.filter_by(period_id=period.id, agent_name=agent_name).first()
    if not agent:
        agent = AgentCommission(
            period_id=period.id, agent_name=agent_name,
            units_cleared=1, total_cleared_debt=enrolled_debt, cancellation_rate=0.0,
            hourly_draw=0.0, raw_tier=1, adjusted_tier=1, tier_rate=0.01,
            gross_commission=round(enrolled_debt * 0.01, 2), clawback_amount=0.0,
            net_commission=round(enrolled_debt * 0.01, 2), payout=round(enrolled_debt * 0.01, 2),
            payout_type="commission", source="history_import", notes="",
        )
        db.session.add(agent)
        db.session.flush()
    db.session.add(ClientRecord(
        period_id=period.id, agent_commission_id=agent.id,
        crm_id=crm_id, agent_name=agent_name, client_name="Katherine Kuschtsch",
        enrolled_debt=enrolled_debt, is_cleared=True,
        first_payment_cleared_date="2026-01-15", payments_made=2, pay_freq="Monthly",
    ))
    db.session.commit()
    return period, agent


class TestClawbackUsesOriginallyRecordedEnrolledDebt:
    def test_clawback_amount_and_display_use_original_debt_not_the_later_crm_row(self, app, db, client):
        _seed_originally_cleared_client(db, crm_id="1208754105", agent_name="Adam Elqaza", enrolled_debt=30_688.0)

        # A later CRM export shows this same crm_id dropping -- with a DIFFERENT
        # (lower) Enrolled Debt than what was actually paid on.
        crm_bytes = _crm_csv_bytes([{
            "ID": "1208754105", "Sales Rep": "Adam Elqaza", "Full Name": "Katherine Kuschtsch",
            "1st Payment Cleared Date": "2026-01-15", "Dropped Date": "07/30/2026",
            "Status": "Cancelled", "Enrolled Debt": "2664.62", "# NSF": "1",
            "Payments Made": "1", "Pay Freq.": "Monthly",
        }])
        resp = client.post(
            "/upload-crm", data={"csv_file": (io.BytesIO(crm_bytes), "crm.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        assert resp.status_code == 200

        with app.app_context():
            clawback_row = ClientRecord.query.filter_by(crm_id="1208754105", clawback_applied=True).first()
            assert clawback_row is not None
            # Displayed Enrolled Debt matches what was ACTUALLY paid on ($30,688),
            # not the later CRM row's $2,664.62.
            assert clawback_row.enrolled_debt == 30_688.0
            # Clawback math used $30,688 too -- agent had no OTHER cleared units
            # that January in this file, so the whole original $306.88 (1% Tier 1
            # of 30,688) commission is clawed back, not 1% of $2,664.62 ($26.65).
            assert clawback_row.clawback_amount == 306.88

    def test_falls_back_to_the_files_own_debt_when_nothing_is_known_yet(self):
        """No known_enrolled_debt_by_crm_id entry for this crm_id at all (a
        client clearing and dropping for the very first time, nothing saved
        in the DB yet to compare against) -- must still use the row's own
        Enrolled Debt rather than erroring out or dropping the clawback.
        Exercised directly against the parser (require_prior_payment_evidence
        off) since app/'s own route policy would otherwise reclassify a
        same-file, no-prior-history "clears and drops in one row" client away
        entirely before this code path is even reached -- see
        commission_core/crm_parser.py's module docstring on that guard."""
        from commission_core.crm_parser import parse_crm_and_calculate

        crm_bytes = _crm_csv_bytes([{
            "ID": "999", "Sales Rep": "Agent A", "Full Name": "Same File Client",
            "1st Payment Cleared Date": "01/10/2026", "Dropped Date": "07/30/2026",
            "Status": "Cancelled", "Enrolled Debt": "5000", "# NSF": "0",
            "Payments Made": "1", "Pay Freq.": "Monthly",
        }])
        periods = parse_crm_and_calculate(
            crm_bytes, "crm.csv", require_prior_payment_evidence=False,
            known_enrolled_debt_by_crm_id={},  # nothing known yet
        )
        clawback_clients = [
            c for p in periods for r in p["results"] for c in r.get("_clawback_clients", [])
        ]
        assert len(clawback_clients) == 1
        assert clawback_clients[0]["enrolled_debt"] == 5000.0
