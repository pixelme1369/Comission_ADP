"""Regression tests for merging "Clawbacks Applied This Period" and "Cordoba
Charge back" into one table on both the admin and agent-facing detail pages
(owner request, confirmed): a "Cordoba Charge back" Yes/No column on the
clawbacks table instead of two separate page sections. A client Cordoba
flags as charged back but that hasn't actually had anything deducted yet
must still show up (as a $0.00 row marked Yes), never silently disappear.
"""

from agent_portal.models import (
    Agent, AgentAlias, AgentCommission, ClientRecord, CommissionPeriod, CordobaChargebackEntry,
)
from test_admin_period_clawback_files import (
    _make_admin, _make_agent_commission, _make_clawback_client, _make_period, _login,
)


def test_matched_clawback_shows_yes_admin_view(app, db, client):
    with app.app_context():
        _make_admin(db)
        period = _make_period(db)
        row = _make_agent_commission(db, period, "Josh Hallwork", clawback_amount=168.66)
        _make_clawback_client(
            db, period, row, "Josh Hallwork", "1202392081", "Shelleen Roseborough",
            clawback_amount=168.66,
        )
        db.session.add(CordobaChargebackEntry(
            crm_id="1202392081", agent_name="Josh Hallwork", period_label=period.period_label,
            client_name="Shelleen Roseborough", marketing_payout_debt=16_866.0,
        ))
        db.session.commit()
        period_id, row_id = period.id, row.id

    _login(client, "admin@example.com")
    resp = client.get(f"/admin/period/{period_id}/agent/{row_id}")
    assert resp.status_code == 200
    assert b"Clawbacks Applied This Period (1)" in resp.data
    assert b"Cordoba Charge back (" not in resp.data  # old separate section gone
    assert b"Shelleen Roseborough" in resp.data
    assert b"-$168.66" in resp.data


def test_cordoba_only_entry_still_shows_with_zero_dollars_admin_view(app, db, client):
    with app.app_context():
        _make_admin(db)
        period = _make_period(db)
        row = _make_agent_commission(db, period, "Josh Hallwork")
        # A regular cleared client (backfills display fields) with NO clawback applied.
        db.session.add(ClientRecord(
            period_id=period.id, agent_commission_id=row.id,
            crm_id="555", agent_name="Josh Hallwork", client_name="Sam Smith",
            enrolled_debt=40_000.0, is_cleared=True,
            first_payment_cleared_date="2026-06-10", pay_freq="Monthly", payments_made=1,
        ))
        db.session.add(CordobaChargebackEntry(
            crm_id="555", agent_name="Josh Hallwork", period_label=period.period_label,
            client_name="Sam Smith", marketing_payout_debt=40_000.0,
        ))
        db.session.commit()
        period_id, row_id = period.id, row.id

    _login(client, "admin@example.com")
    resp = client.get(f"/admin/period/{period_id}/agent/{row_id}")
    assert resp.status_code == 200
    assert b"Clawbacks Applied This Period (1)" in resp.data
    assert b"Sam Smith" in resp.data
    assert b"-$0.00" in resp.data
    assert b"$40,000.00" in resp.data  # backfilled from the real ClientRecord


def test_matched_clawback_shows_yes_agent_facing_view(app, db, client):
    with app.app_context():
        admin = Agent(email="josh@example.com", display_name="Josh Hallwork", is_admin=False)
        admin.set_password("pw12345")
        db.session.add(admin)
        db.session.flush()
        db.session.add(AgentAlias(agent_id=admin.id, agent_name="Josh Hallwork"))
        period = _make_period(db)
        row = _make_agent_commission(db, period, "Josh Hallwork", clawback_amount=168.66)
        _make_clawback_client(
            db, period, row, "Josh Hallwork", "1202392081", "Shelleen Roseborough",
            clawback_amount=168.66,
        )
        db.session.add(CordobaChargebackEntry(
            crm_id="1202392081", agent_name="Josh Hallwork", period_label=period.period_label,
            client_name="Shelleen Roseborough", marketing_payout_debt=16_866.0,
        ))
        db.session.commit()
        period_id, row_id = period.id, row.id

    _login(client, "josh@example.com")
    resp = client.get(f"/portal/period/{period_id}/agent/{row_id}")
    assert resp.status_code == 200
    assert b"Clawbacks Applied This Period (1)" in resp.data
    assert b"Cordoba Charge back (" not in resp.data
    assert b"Shelleen Roseborough" in resp.data
