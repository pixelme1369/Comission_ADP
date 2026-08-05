"""Regression tests for merging "Clawbacks Applied This Period" and "Cordoba
Charge back" into one table on the agent detail page (owner request,
confirmed): a "Cordoba Charge back" Yes/No column on the clawbacks table
instead of two separate page sections. A client Cordoba flags as charged
back but that hasn't actually had anything deducted yet must still show up
(as a $0.00 row marked Yes), never silently disappear from the page.
"""

from app.models import (
    AgentCommission, ClientRecord, CommissionPeriod, CordobaChargebackEntry,
)


def _make_period_with_agent(db, period_label="2026-07", agent_name="Maria"):
    period = CommissionPeriod(period_label=period_label, filename="crm.csv", total_agents=1)
    db.session.add(period)
    db.session.flush()
    agent = AgentCommission(
        period_id=period.id, agent_name=agent_name,
        units_cleared=10, total_cleared_debt=100_000.0, cancellation_rate=0.0,
        hourly_draw=0.0, raw_tier=1, adjusted_tier=1, tier_rate=0.01,
        gross_commission=1_000.0, clawback_amount=375.0, net_commission=625.0,
        payout=625.0, payout_type="commission", source="crm", notes="",
    )
    db.session.add(agent)
    db.session.flush()
    db.session.commit()
    return period, agent


def test_matched_clawback_shows_yes(app, db, client):
    period, agent = _make_period_with_agent(db)
    db.session.add(ClientRecord(
        period_id=period.id, agent_commission_id=agent.id,
        crm_id="111", agent_name="Maria", client_name="John Doe",
        enrolled_debt=30_000.0, is_cleared=False, is_cancelled=True,
        clawback_applied=True, clawback_period_id=period.id, clawback_amount=375.0,
        dropped_date="07/20/2026", pay_freq="Monthly", payments_made=1,
    ))
    db.session.add(CordobaChargebackEntry(
        crm_id="111", agent_name="Maria", period_label="2026-07",
        client_name="John Doe", marketing_payout_debt=30_000.0,
    ))
    db.session.commit()

    resp = client.get(f"/period/{period.id}/agent/{agent.id}")
    assert resp.status_code == 200
    assert b"Clawbacks Applied This Period (1)" in resp.data
    # The old separate section must be gone.
    assert b"Cordoba Charge back (" not in resp.data
    # And the merged row shows the match.
    assert b"John Doe" in resp.data
    assert b"-$375.00" in resp.data


def test_unmatched_clawback_shows_no(app, db, client):
    period, agent = _make_period_with_agent(db)
    db.session.add(ClientRecord(
        period_id=period.id, agent_commission_id=agent.id,
        crm_id="222", agent_name="Maria", client_name="Jane Roe",
        enrolled_debt=25_000.0, is_cleared=False, is_cancelled=True,
        clawback_applied=True, clawback_period_id=period.id, clawback_amount=250.0,
        dropped_date="07/15/2026", pay_freq="Monthly", payments_made=1,
    ))
    db.session.commit()

    resp = client.get(f"/period/{period.id}/agent/{agent.id}")
    assert resp.status_code == 200
    assert b"Jane Roe" in resp.data
    assert b"-$250.00" in resp.data


def test_cordoba_only_entry_still_shows_with_zero_dollars(app, db, client):
    """The early-warning case: Cordoba's file lists this client as charged
    back, but nothing has been deducted yet (no clawback_applied row).
    Must still appear -- not silently dropped."""
    period, agent = _make_period_with_agent(db)
    # A regular cleared client for this crm_id (backfills display fields),
    # but no clawback has actually been applied.
    db.session.add(ClientRecord(
        period_id=period.id, agent_commission_id=agent.id,
        crm_id="333", agent_name="Maria", client_name="Sam Smith",
        enrolled_debt=40_000.0, is_cleared=True,
        first_payment_cleared_date="2026-06-10", pay_freq="Monthly", payments_made=1,
    ))
    db.session.add(CordobaChargebackEntry(
        crm_id="333", agent_name="Maria", period_label="2026-07",
        client_name="Sam Smith", marketing_payout_debt=40_000.0,
    ))
    db.session.commit()

    resp = client.get(f"/period/{period.id}/agent/{agent.id}")
    assert resp.status_code == 200
    assert b"Clawbacks Applied This Period (1)" in resp.data
    assert b"Sam Smith" in resp.data
    assert b"-$0.00" in resp.data
    assert b"$40,000.00" in resp.data  # backfilled from the real ClientRecord
