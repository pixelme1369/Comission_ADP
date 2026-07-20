"""Integration tests for the Cordoba Chargebacks flow — including the regression
test for the double-clawback bug: a client already clawed back via a CRM upload
(or a history-import 'To subtract' row) must NOT be clawed back again when the
Cordoba Chargebacks tab later lists the same client."""

from types import SimpleNamespace

import pytest

from app.models import (
    CommissionPeriod, AgentCommission, ClientRecord,
    CordobaPaidClient, CordobaChargedBackClient,
)
from app.routes import _apply_cordoba_chargebacks

FAKE_FILE = SimpleNamespace(filename="cordoba_payouts.xlsx")

CRM_ID = "4478112"


def chargeback_row(crm_id=CRM_ID, name="John Doe"):
    # Matches what cordoba_parser.py actually extracts from the Chargebacks tab
    # (owner policy, July 2026: only ID matters; Full Name is read for display only).
    return {"crm_id": crm_id, "client_name": name}


def parsed(rows):
    return {"paid_ids": [], "chargebacks": rows, "errors": []}


def seed_paid_june_client(db, cordoba_confirmed=True):
    """June 2026: agent Maria, 25 units, $500k -> Tier 2, $6,250 gross.
    One of those clients is John Doe (CRM_ID), $30k debt."""
    period = CommissionPeriod(period_label="2026-06", filename="crm.csv", total_agents=1)
    db.session.add(period)
    db.session.flush()
    agent = AgentCommission(
        period_id=period.id, agent_name="Maria",
        units_cleared=25, total_cleared_debt=500_000.0, cancellation_rate=0.0,
        hourly_draw=0.0, raw_tier=2, adjusted_tier=2, tier_rate=0.0125,
        gross_commission=6_250.0, clawback_amount=0.0, net_commission=6_250.0,
        payout=6_250.0, payout_type="commission", source="crm", notes="",
    )
    db.session.add(agent)
    db.session.flush()
    db.session.add(ClientRecord(
        period_id=period.id, agent_commission_id=agent.id,
        crm_id=CRM_ID, agent_name="Maria", client_name="John Doe",
        enrolled_debt=30_000.0, is_cleared=True,
        first_payment_cleared_date="06/10/2026", dropped_date="08/03/2026",
        pay_freq="Monthly", payments_made=1,
    ))
    if cordoba_confirmed:
        db.session.add(CordobaPaidClient(crm_id=CRM_ID, client_name="John Doe",
                                         source="first_pays"))
    db.session.commit()
    return period, agent


def test_chargeback_applied_once_in_dropped_month(db):
    seed_paid_june_client(db)

    applied, total, _, _, _, _ = _apply_cordoba_chargebacks(FAKE_FILE, parsed([chargeback_row()]))
    db.session.commit()

    assert applied == 1
    assert total == pytest.approx(375.0)  # 25->24 units, same tier: 30,000 x 1.25%
    aug = CommissionPeriod.query.filter_by(period_label="2026-08").one()
    agent_row = AgentCommission.query.filter_by(period_id=aug.id, agent_name="Maria").one()
    assert agent_row.clawback_amount == pytest.approx(375.0)
    assert CordobaChargedBackClient.query.filter_by(crm_id=CRM_ID).count() == 1


def test_reuploading_same_chargebacks_file_is_a_noop(db):
    seed_paid_june_client(db)
    _apply_cordoba_chargebacks(FAKE_FILE, parsed([chargeback_row()]))
    db.session.commit()

    applied, total, _, _, _, _ = _apply_cordoba_chargebacks(FAKE_FILE, parsed([chargeback_row()]))
    assert (applied, total) == (0, 0.0)


def test_client_already_clawed_back_by_crm_upload_is_not_clawed_again(db):
    """REGRESSION (C1): CRM export reflected the drop first and already deducted
    Maria $375 in August. The Cordoba Chargebacks tab arriving later must skip
    this client instead of deducting another $375."""
    period, agent = seed_paid_june_client(db)

    aug = CommissionPeriod(period_label="2026-08", filename="crm.csv", total_agents=1)
    db.session.add(aug)
    db.session.flush()
    aug_agent = AgentCommission(
        period_id=aug.id, agent_name="Maria",
        units_cleared=0, total_cleared_debt=0.0, cancellation_rate=0.0,
        hourly_draw=0.0, raw_tier=0, adjusted_tier=0, tier_rate=0.0,
        gross_commission=0.0, clawback_amount=375.0, net_commission=0.0,
        payout=0.0, payout_type="none", source="crm", notes="",
    )
    db.session.add(aug_agent)
    db.session.flush()
    # The CRM-driven clawback record for the same client
    db.session.add(ClientRecord(
        period_id=aug.id, agent_commission_id=aug_agent.id,
        crm_id=CRM_ID, agent_name="Maria", client_name="John Doe",
        enrolled_debt=30_000.0, is_cleared=False, is_cancelled=True,
        clawback_applied=True, clawback_period_id=aug.id, clawback_amount=375.0,
    ))
    db.session.commit()

    applied, total, _, _, skipped_already, _ = _apply_cordoba_chargebacks(
        FAKE_FILE, parsed([chargeback_row()]))
    db.session.commit()

    assert (applied, total) == (0, 0.0)
    assert skipped_already == [f"John Doe (ID {CRM_ID})"]
    refreshed = db.session.get(AgentCommission, aug_agent.id)
    assert refreshed.clawback_amount == pytest.approx(375.0)  # unchanged, not 750


def test_never_commissioned_client_is_skipped(db):
    seed_paid_june_client(db)
    applied, total, skipped_not_comm, _, _, _ = _apply_cordoba_chargebacks(
        FAKE_FILE, parsed([chargeback_row(crm_id="9999999", name="Unknown Person")]))
    assert (applied, total) == (0, 0.0)
    assert skipped_not_comm == ["Unknown Person (ID 9999999)"]


def test_unconfirmed_payout_is_skipped(db):
    seed_paid_june_client(db, cordoba_confirmed=False)
    applied, total, _, skipped_unconfirmed, _, _ = _apply_cordoba_chargebacks(
        FAKE_FILE, parsed([chargeback_row()]))
    assert (applied, total) == (0, 0.0)
    assert skipped_unconfirmed == [f"John Doe (ID {CRM_ID})"]


def test_skipped_when_we_have_no_dropped_date_yet(db):
    """Owner policy (confirmed July 2026): the Chargebacks tab's own Dropped Date
    column is ignored — only our own CRM-recorded dropped_date places the deduction.
    A client with no dropped_date on file yet must be skipped, not clawed back into
    their cleared month or some other guess."""
    seed_paid_june_client(db)
    ClientRecord.query.filter_by(crm_id=CRM_ID).update({"dropped_date": None})
    db.session.commit()

    applied, total, _, _, _, skipped_no_date = _apply_cordoba_chargebacks(
        FAKE_FILE, parsed([chargeback_row()]))
    assert (applied, total) == (0, 0.0)
    assert skipped_no_date == [f"John Doe (ID {CRM_ID})"]


def test_marketing_payout_debt_column_is_never_used(db):
    """Owner policy (confirmed July 2026): the Chargebacks tab's Marketing Payout Debt
    column is ignored entirely by the actual clawback deduction — client debt comes
    only from our own CRM-recorded Enrolled Debt. If we don't have one, the clawback
    computes to $0 (no fallback to the file's figure). The parser does now also
    extract this column (owner request), but only to feed the separate, display-only
    _list_cordoba_marketing_payout_debt / CordobaMarketingPayoutDebtEntry listing —
    _apply_cordoba_chargebacks itself never reads it."""
    period, agent = seed_paid_june_client(db)
    ClientRecord.query.filter_by(crm_id=CRM_ID).update({"enrolled_debt": 0.0})
    db.session.commit()

    applied, total, _, _, _, _ = _apply_cordoba_chargebacks(FAKE_FILE, parsed([chargeback_row()]))
    assert (applied, total) == (0, 0.0)
