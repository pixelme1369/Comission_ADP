"""Covers the period-wide "Clawback Files" table on the admin period detail
page (routes_admin.py::period_detail) — every client across ALL agents in a
period who was clawed back, in one audit view, instead of having to open
each agent's own detail page to see their slice of it. Pure read/display:
queries the exact same ClientRecord rows (clawback_applied=True,
period_id=<this period>) crm_parser.py already writes via ingest.py — no
parser or clawback-math changes."""

from agent_portal.models import Agent, AgentCommission, ClientRecord, CommissionPeriod


def _make_admin(db, email="admin@example.com"):
    agent = Agent(email=email, display_name="Admin", is_admin=True)
    agent.set_password("pw12345")
    db.session.add(agent)
    db.session.commit()
    return agent


def _make_period(db, label="2026-07"):
    period = CommissionPeriod(period_label=label, filename="test.csv", total_agents=0)
    db.session.add(period)
    db.session.flush()
    return period


def _make_agent_commission(db, period, agent_name, clawback_amount=0.0, gross=1000.0):
    row = AgentCommission(
        period_id=period.id, agent_name=agent_name, units_cleared=5,
        total_cleared_debt=50_000.0, cancellation_rate=0.0, hourly_draw=0.0,
        raw_tier=1, adjusted_tier=1, tier_rate=0.01, gross_commission=gross,
        clawback_amount=clawback_amount, net_commission=max(0.0, gross - clawback_amount),
        payout=max(0.0, gross - clawback_amount), payout_type="commission",
    )
    db.session.add(row)
    db.session.flush()
    return row


def _make_clawback_client(db, period, agent_commission, agent_name, crm_id, client_name,
                           clawback_amount, first_payment_cleared_date="04/10/2026",
                           dropped_date="07/23/2026", payments_made=1, pay_freq=""):
    record = ClientRecord(
        period_id=period.id, agent_commission_id=agent_commission.id,
        crm_id=crm_id, agent_name=agent_name, client_name=client_name,
        first_payment_cleared_date=first_payment_cleared_date, dropped_date=dropped_date,
        payments_made=payments_made, pay_freq=pay_freq, enrolled_debt=16_866.0,
        is_cleared=False, is_pending=False, is_cancelled=True,
        commission_on_client=0.0, clawback_applied=True,
        clawback_period_id=period.id, clawback_amount=clawback_amount,
    )
    db.session.add(record)
    return record


def _login(client, email, password="pw12345"):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=True)


class TestClawbackFilesSection:
    def test_shows_clawback_clients_across_all_agents_in_the_period(self, app, db, client):
        with app.app_context():
            _make_admin(db)
            period = _make_period(db)
            row_a = _make_agent_commission(db, period, "Josh Hallwork", clawback_amount=168.66)
            row_b = _make_agent_commission(db, period, "Alon Yomorta", clawback_amount=50.0)
            _make_clawback_client(
                db, period, row_a, "Josh Hallwork", "1202392081", "Shelleen Roseborough",
                clawback_amount=168.66,
            )
            _make_clawback_client(
                db, period, row_b, "Alon Yomorta", "999", "Another Client",
                clawback_amount=50.0,
            )
            db.session.commit()
            period_id = period.id

        _login(client, "admin@example.com")
        resp = client.get(f"/admin/period/{period_id}")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)

        assert "Clawback Files (2)" in body
        assert "-$218.66 total" in body  # 168.66 + 50.00
        assert "Shelleen Roseborough" in body
        assert "Another Client" in body
        assert "Josh Hallwork" in body
        assert "Alon Yomorta" in body
        assert "1202392081" in body

    def test_matches_the_exact_reported_example(self, app, db, client):
        """The row from the actual bug report: cleared 04/10/2026, dropped
        07/23/2026 (different months, so not a same-month cancel), 1 payment
        made with no Pay Freq. on file (falls back to the 3-payment safe
        threshold) — below threshold, so it's a real clawback and must show
        up here."""
        with app.app_context():
            _make_admin(db)
            period = _make_period(db)
            row = _make_agent_commission(db, period, "Josh Hallwork", clawback_amount=168.66)
            _make_clawback_client(
                db, period, row, "Josh Hallwork", "1202392081", "Shelleen Roseborough",
                clawback_amount=168.66, first_payment_cleared_date="04/10/2026",
                dropped_date="07/23/2026", payments_made=1, pay_freq="",
            )
            db.session.commit()
            period_id = period.id

        _login(client, "admin@example.com")
        resp = client.get(f"/admin/period/{period_id}")
        body = resp.get_data(as_text=True)
        assert "Clawback Files (1)" in body
        assert "Shelleen Roseborough" in body
        assert "04/10/2026" in body
        assert "07/23/2026" in body

    def test_agent_link_points_at_the_right_agent_detail_page(self, app, db, client):
        with app.app_context():
            _make_admin(db)
            period = _make_period(db)
            row = _make_agent_commission(db, period, "Josh Hallwork", clawback_amount=168.66)
            _make_clawback_client(
                db, period, row, "Josh Hallwork", "1202392081", "Shelleen Roseborough",
                clawback_amount=168.66,
            )
            db.session.commit()
            period_id, agent_commission_id = period.id, row.id

        _login(client, "admin@example.com")
        resp = client.get(f"/admin/period/{period_id}")
        body = resp.get_data(as_text=True)
        assert f"/admin/period/{period_id}/agent/{agent_commission_id}" in body

    def test_section_absent_when_no_clawbacks_in_the_period(self, app, db, client):
        with app.app_context():
            _make_admin(db)
            period = _make_period(db)
            _make_agent_commission(db, period, "Clean Agent", clawback_amount=0.0)
            db.session.commit()
            period_id = period.id

        _login(client, "admin@example.com")
        resp = client.get(f"/admin/period/{period_id}")
        body = resp.get_data(as_text=True)
        assert "Clawback Files" not in body

    def test_non_admin_cannot_reach_it(self, app, db, client):
        with app.app_context():
            agent = Agent(email="agent@example.com", display_name="Regular Agent", is_admin=False)
            agent.set_password("pw12345")
            db.session.add(agent)
            period = _make_period(db)
            db.session.commit()
            period_id = period.id

        _login(client, "agent@example.com")
        resp = client.get(f"/admin/period/{period_id}", follow_redirects=True)
        assert b"Admin access required" in resp.data
