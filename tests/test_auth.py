"""Auth/access-control regression tests. The core guarantee this file protects:
an agent can never view or export another agent's commission data, and none of
the admin-only routes (upload/delete/reset/all-agents views) are reachable
without an admin login."""

from app.models import CommissionPeriod, AgentCommission
from app.calculator import get_fixed_rate
from tests.conftest import login


def seed_period_with_two_agents(db):
    period = CommissionPeriod(period_label="2026-06", filename="crm.csv", total_agents=2)
    db.session.add(period)
    db.session.flush()

    agent_a = AgentCommission(
        period_id=period.id, agent_name="Agent A",
        units_cleared=10, total_cleared_debt=100_000.0, cancellation_rate=0.0,
        hourly_draw=0.0, raw_tier=1, adjusted_tier=1, tier_rate=0.01,
        gross_commission=1_000.0, clawback_amount=0.0, net_commission=1_000.0,
        payout=1_000.0, payout_type="commission", source="crm", notes="",
    )
    agent_b = AgentCommission(
        period_id=period.id, agent_name="Agent B",
        units_cleared=15, total_cleared_debt=150_000.0, cancellation_rate=0.0,
        hourly_draw=0.0, raw_tier=1, adjusted_tier=1, tier_rate=0.01,
        gross_commission=1_500.0, clawback_amount=0.0, net_commission=1_500.0,
        payout=1_500.0, payout_type="commission", source="crm", notes="",
    )
    db.session.add_all([agent_a, agent_b])
    db.session.commit()
    return period, agent_a, agent_b


class TestLogin:
    def test_login_success(self, client, make_user):
        make_user("admin", "correct-horse", is_admin=True)
        resp = login(client, "admin", "correct-horse")
        assert resp.status_code == 200
        assert b"Invalid username or password" not in resp.data

    def test_login_wrong_password(self, client, make_user):
        make_user("admin", "correct-horse", is_admin=True)
        resp = login(client, "admin", "wrong-password")
        assert b"Invalid username or password" in resp.data

    def test_login_unknown_username(self, client):
        resp = login(client, "ghost", "whatever")
        assert b"Invalid username or password" in resp.data

    def test_login_disabled_account_rejected(self, client, make_user):
        make_user("agent_a", "pw", agent_name="Agent A", active=False)
        resp = login(client, "agent_a", "pw")
        assert b"Invalid username or password" in resp.data

    def test_logout_clears_session(self, client, make_user):
        make_user("admin", "pw", is_admin=True)
        login(client, "admin", "pw")
        client.post("/logout")
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in (302, 401, 403)


class TestAnonymousAccess:
    def test_anonymous_redirected_to_login(self, client, db):
        period, agent_a, _ = seed_period_with_two_agents(db)
        for url in ["/", "/history", f"/period/{period.id}", f"/period/{period.id}/agent/{agent_a.id}"]:
            resp = client.get(url, follow_redirects=False)
            assert resp.status_code == 302
            assert "/login" in resp.headers["Location"]


class TestAdminOnlyRoutes:
    def test_agent_cannot_reach_admin_routes(self, client, db, make_user):
        period, agent_a, _ = seed_period_with_two_agents(db)
        make_user("agent_a", "pw", agent_name="Agent A")
        login(client, "agent_a", "pw")

        for url in ["/", "/history", f"/period/{period.id}", f"/period/{period.id}/export",
                    f"/period/{period.id}/export-by-agent"]:
            resp = client.get(url)
            assert resp.status_code == 403, url

        assert client.post("/reset-all").status_code == 403
        assert client.post(f"/period/{period.id}/delete").status_code == 403

    def test_admin_can_reach_admin_routes(self, client, db, make_user):
        period, _, _ = seed_period_with_two_agents(db)
        make_user("admin", "pw", is_admin=True)
        login(client, "admin", "pw")

        assert client.get("/").status_code == 200
        assert client.get("/history").status_code == 200
        assert client.get(f"/period/{period.id}").status_code == 200


class TestOwnershipEnforcement:
    def test_agent_cannot_view_another_agents_page(self, client, db, make_user):
        period, agent_a, agent_b = seed_period_with_two_agents(db)
        make_user("agent_a", "pw", agent_name="Agent A")
        login(client, "agent_a", "pw")

        resp = client.get(f"/period/{period.id}/agent/{agent_b.id}")
        assert resp.status_code == 403

    def test_agent_cannot_export_another_agents_data(self, client, db, make_user):
        period, agent_a, agent_b = seed_period_with_two_agents(db)
        make_user("agent_a", "pw", agent_name="Agent A")
        login(client, "agent_a", "pw")

        resp = client.get(f"/period/{period.id}/agent/{agent_b.id}/export")
        assert resp.status_code == 403

    def test_agent_can_view_own_page(self, client, db, make_user):
        period, agent_a, _ = seed_period_with_two_agents(db)
        make_user("agent_a", "pw", agent_name="Agent A")
        login(client, "agent_a", "pw")

        resp = client.get(f"/period/{period.id}/agent/{agent_a.id}")
        assert resp.status_code == 200

    def test_admin_can_view_any_agent_page(self, client, db, make_user):
        period, agent_a, agent_b = seed_period_with_two_agents(db)
        make_user("admin", "pw", is_admin=True)
        login(client, "admin", "pw")

        assert client.get(f"/period/{period.id}/agent/{agent_a.id}").status_code == 200
        assert client.get(f"/period/{period.id}/agent/{agent_b.id}").status_code == 200

    def test_ownership_matches_case_and_whitespace_insensitively(self, client, db, make_user):
        """CRM data entry isn't perfectly consistent — AgentUser.agent_name is the
        canonical display-case name, but AgentCommission.agent_name might have
        different casing/whitespace from a messy export. Ownership must still match."""
        period = CommissionPeriod(period_label="2026-07", filename="crm.csv", total_agents=1)
        db.session.add(period)
        db.session.flush()
        agent = AgentCommission(
            period_id=period.id, agent_name="  alex tambouly ",
            units_cleared=5, total_cleared_debt=50_000.0, cancellation_rate=0.0,
            hourly_draw=0.0, raw_tier=1, adjusted_tier=1, tier_rate=0.02,
            gross_commission=1_000.0, clawback_amount=0.0, net_commission=1_000.0,
            payout=1_000.0, payout_type="commission", source="crm", notes="",
        )
        db.session.add(agent)
        db.session.commit()

        make_user("alex", "pw", agent_name="Alex Tambouly")
        login(client, "alex", "pw")

        resp = client.get(f"/period/{period.id}/agent/{agent.id}")
        assert resp.status_code == 200


class TestMyDashboard:
    def test_agent_sees_only_own_periods(self, client, db, make_user):
        period, agent_a, agent_b = seed_period_with_two_agents(db)
        make_user("agent_a", "pw", agent_name="Agent A")
        login(client, "agent_a", "pw")

        resp = client.get("/my-dashboard")
        assert resp.status_code == 200
        assert b"Agent A" in resp.data
        # Agent B's own net commission figure should not leak onto Agent A's dashboard.
        assert b"1,500.00" not in resp.data

    def test_admin_redirected_away_from_my_dashboard(self, client, db, make_user):
        make_user("admin", "pw", is_admin=True)
        login(client, "admin", "pw")
        resp = client.get("/my-dashboard", follow_redirects=False)
        assert resp.status_code == 302


class TestAdminAgentCreation:
    def test_admin_creates_agent_login(self, client, db, make_user):
        seed_period_with_two_agents(db)
        make_user("admin", "pw", is_admin=True)
        login(client, "admin", "pw")

        resp = client.post("/admin/agents", data={
            "agent_name": "Agent A", "username": "agent_a", "password": "temp-pw",
        }, follow_redirects=True)
        assert resp.status_code == 200

        from app.models import AgentUser
        created = AgentUser.query.filter_by(username="agent_a").first()
        assert created is not None
        assert created.agent_name == "Agent A"
        assert created.password_hash != "temp-pw"  # never store plaintext

    def test_duplicate_username_rejected(self, client, db, make_user):
        seed_period_with_two_agents(db)
        make_user("admin", "pw", is_admin=True)
        make_user("agent_a", "pw2", agent_name="Agent A")
        login(client, "admin", "pw")

        resp = client.post("/admin/agents", data={
            "agent_name": "Agent B", "username": "agent_a", "password": "another-pw",
        }, follow_redirects=True)
        assert b"already taken" in resp.data

    def test_agent_cannot_create_logins(self, client, db, make_user):
        make_user("agent_a", "pw", agent_name="Agent A")
        login(client, "agent_a", "pw")
        assert client.get("/admin/agents").status_code == 403


def test_get_fixed_rate_unaffected_by_normalize_agent_name_refactor():
    """Regression guard for CLAUDE.md's owner-confirmed TestFixedRateOverride policy —
    the normalize_agent_name extraction must not change get_fixed_rate's behavior."""
    assert get_fixed_rate("Alex Tambouly") == 0.02
    assert get_fixed_rate("  alex tambouly  ") == 0.02
    assert get_fixed_rate("Peter Godwin") == 0.0175
    assert get_fixed_rate("Someone Else") is None
