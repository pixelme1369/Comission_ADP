"""Covers the self-serve "Fix Now" migration for databases created before
"Sign in with Google" shipped, where agent.password_hash is still NOT NULL
(see routes_admin.py::run_nullable_password_migration and
_password_column_is_nullable). The actual ALTER TABLE ... DROP NOT NULL only
means something on Postgres — SQLite doesn't support ALTER COLUMN at all — so
these tests mock at the db.session boundary rather than relying on real
column-constraint behavior. The real Postgres path (old NOT NULL schema →
"Fix Now" → blank-password agent creation succeeds) was verified by hand
against a real local Postgres instance when this was built; see the PR/commit
description for that transcript."""

from agent_portal.models import Agent


def _make_admin(db, email="admin@company.com"):
    agent = Agent(email=email, display_name="Admin User", is_admin=True)
    agent.set_password("pw12345")
    db.session.add(agent)
    db.session.commit()
    return agent


def _login_as(client, agent):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(agent.id)
        sess["_fresh"] = True


class TestFixNowCardVisibility:
    def test_card_hidden_on_a_fresh_database(self, app, db, client):
        # conftest's fresh SQLite db is built from the current (nullable) model,
        # so no migration is ever needed for it.
        admin = _make_admin(db)
        _login_as(client, admin)
        resp = client.get("/admin/")
        assert b"Google Sign-In Setup Needed" not in resp.data

    def test_card_shown_when_introspection_reports_not_nullable(self, app, db, client, monkeypatch):
        admin = _make_admin(db)
        _login_as(client, admin)
        monkeypatch.setattr("agent_portal.routes_admin._password_column_is_nullable", lambda: False)
        resp = client.get("/admin/")
        assert b"Google Sign-In Setup Needed" in resp.data
        assert b"Fix Now" in resp.data

    def test_detection_fails_open_on_introspection_error(self, app, db, monkeypatch):
        """If we can't even introspect the schema, don't block the dashboard
        behind a warning that might be wrong — assume no action needed."""
        from agent_portal import routes_admin

        def boom(*a, **kw):
            raise RuntimeError("no introspection access")
        monkeypatch.setattr(routes_admin, "sa_inspect", boom)
        assert routes_admin._password_column_is_nullable() is True


class TestFixNowMigrationRoute:
    def test_successful_migration_flashes_success(self, app, db, client, monkeypatch):
        admin = _make_admin(db)
        _login_as(client, admin)
        monkeypatch.setattr("agent_portal.db.session.execute", lambda *a, **kw: None)
        resp = client.post("/admin/migrate/nullable-password", follow_redirects=True)
        assert resp.status_code == 200
        assert b"Migration applied" in resp.data

    def test_failed_migration_flashes_error_without_crashing(self, app, db, client, monkeypatch):
        admin = _make_admin(db)
        _login_as(client, admin)

        def boom(*a, **kw):
            raise Exception("ALTER COLUMN is not supported on this dialect")
        monkeypatch.setattr("agent_portal.db.session.execute", boom)
        resp = client.post("/admin/migrate/nullable-password", follow_redirects=True)
        assert resp.status_code == 200
        assert b"Migration failed" in resp.data

    def test_non_admin_cannot_run_migration(self, app, db, client):
        agent = Agent(email="agent@company.com", display_name="Regular Agent", is_admin=False)
        agent.set_password("pw12345")
        db.session.add(agent)
        db.session.commit()
        _login_as(client, agent)
        resp = client.post("/admin/migrate/nullable-password", follow_redirects=True)
        assert resp.status_code == 200
        assert b"Admin access required" in resp.data


class TestBlankPasswordCreationFailsGracefully:
    def test_flush_error_is_caught_with_guidance_instead_of_500(self, app, db, client, monkeypatch):
        """Simulates hitting Create Account with a blank password against an
        un-migrated database — reproduces the user-reported Internal Server
        Error and confirms it now degrades to a helpful flash message."""
        admin = _make_admin(db)
        _login_as(client, admin)

        def boom(*a, **kw):
            raise Exception('null value in column "password_hash" violates not-null constraint')
        monkeypatch.setattr("agent_portal.db.session.flush", boom)

        resp = client.post(
            "/admin/agents",
            data={"email": "blank@company.com", "display_name": "Blank Password Agent"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"one-time migration" in resp.data
        assert b"Fix Now" in resp.data
        # And the half-created row must not have been left dangling.
        assert Agent.query.filter_by(email="blank@company.com").first() is None
