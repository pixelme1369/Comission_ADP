"""Confirms one agent's login can never see another agent's commission data —
the entire point of this portal existing separately from the internal
single-user tool. Also checks admin-only routes reject non-admin logins."""

from agent_portal.models import Agent, AgentAlias, CommissionPeriod, AgentCommission


def _make_agent(db, email, display_name, agent_name, is_admin=False, password="pw12345"):
    agent = Agent(email=email, display_name=display_name, is_admin=is_admin)
    agent.set_password(password)
    db.session.add(agent)
    db.session.flush()
    db.session.add(AgentAlias(agent_id=agent.id, agent_name=agent_name))
    db.session.commit()
    return agent


def _make_period_row(db, period_label, agent_name, units=5, gross=1000.0):
    period = CommissionPeriod.query.filter_by(period_label=period_label).first()
    if not period:
        period = CommissionPeriod(period_label=period_label, filename="test.csv", total_agents=1)
        db.session.add(period)
        db.session.flush()
    row = AgentCommission(
        period_id=period.id, agent_name=agent_name, units_cleared=units,
        total_cleared_debt=100_000.0, cancellation_rate=0.0, hourly_draw=0.0,
        raw_tier=1, adjusted_tier=1, tier_rate=0.01, gross_commission=gross,
        clawback_amount=0.0, net_commission=gross, payout=gross, payout_type="commission",
    )
    db.session.add(row)
    db.session.commit()
    return row


def _login(client, email, password="pw12345"):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=True)


class TestAgentScoping:
    def test_dashboard_only_shows_own_rows(self, app, db, client):
        with app.app_context():
            _make_agent(db, "alice@example.com", "Alice", "Alice Agent")
            _make_agent(db, "bob@example.com", "Bob", "Bob Agent")
            _make_period_row(db, "2026-01", "Alice Agent", gross=1234.0)
            _make_period_row(db, "2026-01", "Bob Agent", gross=5678.0)

        _login(client, "alice@example.com")
        resp = client.get("/portal/")
        assert resp.status_code == 200
        assert b"1,234.00" in resp.data
        assert b"5,678.00" not in resp.data

    def test_cannot_view_another_agents_period_detail_by_guessing_url(self, app, db, client):
        with app.app_context():
            _make_agent(db, "alice@example.com", "Alice", "Alice Agent")
            _make_agent(db, "bob@example.com", "Bob", "Bob Agent")
            _make_period_row(db, "2026-01", "Alice Agent")
            bob_row = _make_period_row(db, "2026-01", "Bob Agent")
            bob_row_id = bob_row.id
            bob_period_id = bob_row.period_id

        _login(client, "alice@example.com")
        resp = client.get(f"/portal/period/{bob_period_id}/agent/{bob_row_id}")
        assert resp.status_code == 404

    def test_logged_out_user_redirected_to_login(self, client):
        resp = client.get("/portal/", follow_redirects=True)
        assert b"Log in" in resp.data or resp.request.path == "/login"

    def test_wrong_password_rejected(self, app, db, client):
        with app.app_context():
            _make_agent(db, "alice@example.com", "Alice", "Alice Agent")
        resp = _login(client, "alice@example.com", password="wrong-password")
        assert b"Invalid email or password" in resp.data


class TestCurrentPeriodOnly:
    """Owner policy: agents only ever see the single most recent commission
    period — no browsing prior months from the portal, even for their own
    historical data."""

    def test_dashboard_only_shows_latest_period_not_history(self, app, db, client):
        with app.app_context():
            _make_agent(db, "alice@example.com", "Alice", "Alice Agent")
            _make_period_row(db, "2026-01", "Alice Agent", gross=1111.0)
            _make_period_row(db, "2026-02", "Alice Agent", gross=2222.0)

        _login(client, "alice@example.com")
        resp = client.get("/portal/")
        assert resp.status_code == 200
        assert b"2,222.00" in resp.data
        assert b"1,111.00" not in resp.data

    def test_cannot_view_own_older_period_via_url(self, app, db, client):
        with app.app_context():
            _make_agent(db, "alice@example.com", "Alice", "Alice Agent")
            old_row = _make_period_row(db, "2026-01", "Alice Agent", gross=1111.0)
            old_row_id = old_row.id
            old_period_id = old_row.period_id
            _make_period_row(db, "2026-02", "Alice Agent", gross=2222.0)

        _login(client, "alice@example.com")
        resp = client.get(f"/portal/period/{old_period_id}/agent/{old_row_id}")
        assert resp.status_code == 404


class TestAdminScoping:
    def test_non_admin_cannot_reach_admin_dashboard(self, app, db, client):
        with app.app_context():
            _make_agent(db, "alice@example.com", "Alice", "Alice Agent", is_admin=False)
        _login(client, "alice@example.com")
        resp = client.get("/admin/", follow_redirects=True)
        assert b"Admin access required" in resp.data

    def test_admin_can_reach_admin_dashboard(self, app, db, client):
        with app.app_context():
            _make_agent(db, "saman@example.com", "Saman", "Saman Agent", is_admin=True)
        _login(client, "saman@example.com")
        resp = client.get("/admin/")
        assert resp.status_code == 200
