"""Covers the "Sign in with Google" flow (auth.py::google_login) — the
Google Identity Services button POSTs a signed ID-token JWT to /login/google,
which we verify and then match against our own Agent.email. Google's own
token verification is mocked (agent_portal.auth.google_id_token.verify_oauth2_token)
so these tests never make a real network call, matching the CI environment
having no path to Google's servers."""

from agent_portal.models import Agent, AgentAlias


def _make_agent(db, email, display_name, is_admin=False, with_password=True):
    agent = Agent(email=email, display_name=display_name, is_admin=is_admin)
    if with_password:
        agent.set_password("pw12345")
    db.session.add(agent)
    db.session.commit()
    return agent


def _mock_verify(monkeypatch, claims):
    def fake_verify(token, request, client_id):
        assert client_id == "test-client-id"
        return claims
    monkeypatch.setattr("agent_portal.auth.google_id_token.verify_oauth2_token", fake_verify)


class TestGoogleLogin:
    def test_google_signin_logs_in_a_known_email_with_no_password_set(self, app, db, client, monkeypatch):
        app.config["GOOGLE_CLIENT_ID"] = "test-client-id"
        _make_agent(db, "alon@company.com", "Alon Yomorta", with_password=False)
        _mock_verify(monkeypatch, {"email": "alon@company.com", "email_verified": True})

        resp = client.post("/login/google", data={"credential": "fake-jwt"}, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Log out" in resp.data or b"My Commissions" in resp.data

    def test_google_signin_works_even_when_agent_also_has_a_password(self, app, db, client, monkeypatch):
        app.config["GOOGLE_CLIENT_ID"] = "test-client-id"
        _make_agent(db, "dual@company.com", "Dual Login Agent", with_password=True)
        _mock_verify(monkeypatch, {"email": "dual@company.com", "email_verified": True})

        resp = client.post("/login/google", data={"credential": "fake-jwt"}, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Log out" in resp.data or b"My Commissions" in resp.data

    def test_unknown_google_email_is_rejected(self, app, db, client, monkeypatch):
        app.config["GOOGLE_CLIENT_ID"] = "test-client-id"
        _mock_verify(monkeypatch, {"email": "stranger@gmail.com", "email_verified": True})

        resp = client.post("/login/google", data={"credential": "fake-jwt"}, follow_redirects=True)
        assert resp.status_code == 200
        assert b"No portal account found" in resp.data

    def test_unverified_email_is_rejected(self, app, db, client, monkeypatch):
        app.config["GOOGLE_CLIENT_ID"] = "test-client-id"
        _make_agent(db, "alon@company.com", "Alon Yomorta", with_password=False)
        _mock_verify(monkeypatch, {"email": "alon@company.com", "email_verified": False})

        resp = client.post("/login/google", data={"credential": "fake-jwt"}, follow_redirects=True)
        assert resp.status_code == 200
        assert b"not verified" in resp.data

    def test_invalid_token_is_rejected(self, app, db, client, monkeypatch):
        app.config["GOOGLE_CLIENT_ID"] = "test-client-id"

        def fake_verify(token, request, client_id):
            raise ValueError("bad token")
        monkeypatch.setattr("agent_portal.auth.google_id_token.verify_oauth2_token", fake_verify)

        resp = client.post("/login/google", data={"credential": "garbage"}, follow_redirects=True)
        assert resp.status_code == 200
        assert b"could not be verified" in resp.data

    def test_google_signin_disabled_without_client_id_configured(self, client):
        # GOOGLE_CLIENT_ID defaults to None in the test app config.
        resp = client.post("/login/google", data={"credential": "fake-jwt"}, follow_redirects=True)
        assert resp.status_code == 200
        assert b"not configured" in resp.data

    def test_admin_can_create_agent_with_no_password(self, app, db, client):
        admin = _make_agent(db, "admin@company.com", "Admin User", is_admin=True)
        with client.session_transaction() as sess:
            sess["_user_id"] = str(admin.id)
            sess["_fresh"] = True

        resp = client.post(
            "/admin/agents",
            data={"email": "new@company.com", "display_name": "New Agent", "agent_name": "New Agent"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        created = Agent.query.filter_by(email="new@company.com").first()
        assert created is not None
        assert created.password_hash is None
        assert created.check_password("anything") is False

    def test_admin_creating_agent_with_short_password_is_rejected(self, app, db, client):
        admin = _make_agent(db, "admin@company.com", "Admin User", is_admin=True)
        with client.session_transaction() as sess:
            sess["_user_id"] = str(admin.id)
            sess["_fresh"] = True

        resp = client.post(
            "/admin/agents",
            data={"email": "short@company.com", "display_name": "Short Pw", "password": "abc"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert Agent.query.filter_by(email="short@company.com").first() is None
