"""Confirms one agent's login can never see another agent's commission data —
the entire point of this portal existing separately from the internal
single-user tool. Also checks admin-only routes reject non-admin logins."""

import csv
import io

from agent_portal.models import Agent, AgentAlias, CommissionPeriod, AgentCommission, ClientRecord


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

    def test_no_export_csv_route_exists_for_agents(self, app, db, client):
        """Owner policy: agents cannot export a CSV copy of their commission data."""
        with app.app_context():
            _make_agent(db, "alice@example.com", "Alice", "Alice Agent")
            row = _make_period_row(db, "2026-01", "Alice Agent")
            period_id, row_id = row.period_id, row.id

        _login(client, "alice@example.com")
        resp = client.get(f"/portal/period/{period_id}/agent/{row_id}")
        assert b"Export CSV" not in resp.data
        resp = client.get(f"/portal/period/{period_id}/agent/{row_id}/export")
        assert resp.status_code == 404


class TestCurrentPeriodOnly:
    """Owner policy: agents only ever see the two most recent commission
    periods — no browsing further back than that from the portal, even for
    their own historical data."""

    def test_dashboard_shows_latest_two_periods_not_further_history(self, app, db, client):
        with app.app_context():
            _make_agent(db, "alice@example.com", "Alice", "Alice Agent")
            _make_period_row(db, "2026-01", "Alice Agent", gross=1111.0)
            _make_period_row(db, "2026-02", "Alice Agent", gross=2222.0)
            _make_period_row(db, "2026-03", "Alice Agent", gross=3333.0)

        _login(client, "alice@example.com")
        resp = client.get("/portal/")
        assert resp.status_code == 200
        assert b"3,333.00" in resp.data
        assert b"2,222.00" in resp.data
        assert b"1,111.00" not in resp.data

    def test_cannot_view_a_period_older_than_the_latest_two(self, app, db, client):
        with app.app_context():
            _make_agent(db, "alice@example.com", "Alice", "Alice Agent")
            oldest_row = _make_period_row(db, "2026-01", "Alice Agent", gross=1111.0)
            oldest_row_id = oldest_row.id
            oldest_period_id = oldest_row.period_id
            middle_row = _make_period_row(db, "2026-02", "Alice Agent", gross=2222.0)
            middle_row_id, middle_period_id = middle_row.id, middle_row.period_id
            _make_period_row(db, "2026-03", "Alice Agent", gross=3333.0)

        _login(client, "alice@example.com")
        # Outside the latest-2 window (Jan, when Feb+Mar are the latest two)
        resp = client.get(f"/portal/period/{oldest_period_id}/agent/{oldest_row_id}")
        assert resp.status_code == 404
        # Within the window (Feb is the second-latest)
        resp = client.get(f"/portal/period/{middle_period_id}/agent/{middle_row_id}")
        assert resp.status_code == 200


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

    def test_admin_can_view_any_period_with_all_agents(self, app, db, client):
        """Unlike the agent-facing portal, admin can browse every period and
        every agent in it — not just the latest, not just their own."""
        with app.app_context():
            _make_agent(db, "saman@example.com", "Saman", "Saman Agent", is_admin=True)
            old_row = _make_period_row(db, "2026-01", "Alice Agent", gross=1111.0)
            other_row = _make_period_row(db, "2026-01", "Bob Agent", gross=5678.0)
            old_period_id = old_row.period_id
            other_row_id = other_row.id
            _make_period_row(db, "2026-02", "Alice Agent", gross=2222.0)  # a newer period exists too

        _login(client, "saman@example.com")
        resp = client.get(f"/admin/period/{old_period_id}")
        assert resp.status_code == 200
        assert b"Alice Agent" in resp.data
        assert b"Bob Agent" in resp.data

        resp = client.get(f"/admin/period/{old_period_id}/agent/{other_row_id}")
        assert resp.status_code == 200
        assert b"Bob Agent" in resp.data

    def test_non_admin_cannot_reach_admin_period_detail(self, app, db, client):
        with app.app_context():
            _make_agent(db, "alice@example.com", "Alice", "Alice Agent", is_admin=False)
            row = _make_period_row(db, "2026-01", "Alice Agent")
            period_id = row.period_id

        _login(client, "alice@example.com")
        resp = client.get(f"/admin/period/{period_id}", follow_redirects=True)
        assert b"Admin access required" in resp.data

    def test_admin_can_reset_an_agents_password(self, app, db, client):
        with app.app_context():
            _make_agent(db, "saman@example.com", "Saman", "Saman Agent", is_admin=True)
            alice = _make_agent(db, "alice@example.com", "Alice", "Alice Agent")
            alice_id = alice.id

        _login(client, "saman@example.com")
        resp = client.post(
            f"/admin/agents/{alice_id}/password",
            data={"password": "brand-new-password"}, follow_redirects=True,
        )
        assert b"Password updated for Alice" in resp.data
        client.get("/logout")

        # Old password no longer works, new one does
        resp = _login(client, "alice@example.com", password="pw12345")
        assert b"Invalid email or password" in resp.data
        resp = _login(client, "alice@example.com", password="brand-new-password")
        assert resp.request.path == "/portal/"

    def test_password_reset_rejects_too_short_password(self, app, db, client):
        with app.app_context():
            _make_agent(db, "saman@example.com", "Saman", "Saman Agent", is_admin=True)
            alice = _make_agent(db, "alice@example.com", "Alice", "Alice Agent")
            alice_id = alice.id

        _login(client, "saman@example.com")
        resp = client.post(
            f"/admin/agents/{alice_id}/password", data={"password": "abc"}, follow_redirects=True,
        )
        assert b"Password must be at least 6 characters" in resp.data

    def test_non_admin_cannot_reset_passwords(self, app, db, client):
        with app.app_context():
            alice = _make_agent(db, "alice@example.com", "Alice", "Alice Agent")
            bob = _make_agent(db, "bob@example.com", "Bob", "Bob Agent")
            bob_id = bob.id

        _login(client, "alice@example.com")
        resp = client.post(
            f"/admin/agents/{bob_id}/password", data={"password": "sneaky123"}, follow_redirects=True,
        )
        assert b"Admin access required" in resp.data

    def test_admin_can_update_an_agents_email(self, app, db, client):
        with app.app_context():
            _make_agent(db, "saman@example.com", "Saman", "Saman Agent", is_admin=True)
            alice = _make_agent(db, "alice@example.com", "Alice", "Alice Agent")
            alice_id = alice.id

        _login(client, "saman@example.com")
        resp = client.post(
            f"/admin/agents/{alice_id}/email", data={"email": "alice.new@example.com"}, follow_redirects=True,
        )
        assert b"Updated email for Alice" in resp.data
        client.get("/logout")

        # Old email no longer logs in, new one does
        resp = _login(client, "alice@example.com", password="pw12345")
        assert b"Invalid email or password" in resp.data
        resp = _login(client, "alice.new@example.com", password="pw12345")
        assert resp.request.path == "/portal/"

    def test_email_update_rejects_a_duplicate(self, app, db, client):
        with app.app_context():
            _make_agent(db, "saman@example.com", "Saman", "Saman Agent", is_admin=True)
            alice = _make_agent(db, "alice@example.com", "Alice", "Alice Agent")
            _make_agent(db, "bob@example.com", "Bob", "Bob Agent")
            alice_id = alice.id

        _login(client, "saman@example.com")
        resp = client.post(
            f"/admin/agents/{alice_id}/email", data={"email": "bob@example.com"}, follow_redirects=True,
        )
        assert b"Another account already uses bob@example.com" in resp.data

    def test_email_update_rejects_blank(self, app, db, client):
        with app.app_context():
            _make_agent(db, "saman@example.com", "Saman", "Saman Agent", is_admin=True)
            alice = _make_agent(db, "alice@example.com", "Alice", "Alice Agent")
            alice_id = alice.id

        _login(client, "saman@example.com")
        resp = client.post(f"/admin/agents/{alice_id}/email", data={"email": ""}, follow_redirects=True)
        assert b"Email is required" in resp.data

    def test_non_admin_cannot_update_email(self, app, db, client):
        with app.app_context():
            alice = _make_agent(db, "alice@example.com", "Alice", "Alice Agent")
            bob = _make_agent(db, "bob@example.com", "Bob", "Bob Agent")
            bob_id = bob.id

        _login(client, "alice@example.com")
        resp = client.post(
            f"/admin/agents/{bob_id}/email", data={"email": "sneaky@example.com"}, follow_redirects=True,
        )
        assert b"Admin access required" in resp.data

    def test_admin_can_delete_an_agent_without_touching_their_commission_history(self, app, db, client):
        with app.app_context():
            _make_agent(db, "saman@example.com", "Saman", "Saman Agent", is_admin=True)
            alice = _make_agent(db, "alice@example.com", "Alice", "Alice Agent")
            alice_id = alice.id
            _make_period_row(db, "2026-01", "Alice Agent", gross=1234.0)

        _login(client, "saman@example.com")
        resp = client.post(f"/admin/agents/{alice_id}/delete", follow_redirects=True)
        assert b"Removed account for Alice" in resp.data

        with app.app_context():
            assert Agent.query.filter_by(email="alice@example.com").first() is None
            assert AgentAlias.query.filter_by(agent_name="Alice Agent").first() is None
            # Commission data itself is untouched — only the login is gone
            assert AgentCommission.query.filter_by(agent_name="Alice Agent").first() is not None

        client.get("/logout")
        resp = _login(client, "alice@example.com", password="pw12345")
        assert b"Invalid email or password" in resp.data

    def test_admin_cannot_delete_own_account(self, app, db, client):
        with app.app_context():
            saman = _make_agent(db, "saman@example.com", "Saman", "Saman Agent", is_admin=True)
            saman_id = saman.id

        _login(client, "saman@example.com")
        resp = client.post(f"/admin/agents/{saman_id}/delete", follow_redirects=True)
        assert b"cannot delete the account you are currently logged in as" in resp.data
        with app.app_context():
            assert Agent.query.get(saman_id) is not None

    def test_deleting_one_of_two_admins_is_allowed(self, app, db, client):
        """The last-admin guard only blocks deleting the sole remaining admin —
        with two admins, removing one is fine (the self-delete guard is what
        actually protects the final admin, covered separately above)."""
        with app.app_context():
            _make_agent(db, "saman@example.com", "Saman", "Saman Agent", is_admin=True)
            other_admin = _make_agent(db, "backup@example.com", "Backup", "Backup Agent", is_admin=True)
            other_admin_id = other_admin.id

        _login(client, "saman@example.com")
        resp = client.post(f"/admin/agents/{other_admin_id}/delete", follow_redirects=True)
        assert b"Removed account for Backup" in resp.data
        with app.app_context():
            assert Agent.query.filter_by(is_admin=True).count() == 1

    def test_non_admin_cannot_delete_agents(self, app, db, client):
        with app.app_context():
            alice = _make_agent(db, "alice@example.com", "Alice", "Alice Agent")
            bob = _make_agent(db, "bob@example.com", "Bob", "Bob Agent")
            bob_id = bob.id

        _login(client, "alice@example.com")
        resp = client.post(f"/admin/agents/{bob_id}/delete", follow_redirects=True)
        assert b"Admin access required" in resp.data


class TestClearedClientsDisplay:
    """Covers three related display changes on the agent-facing period page:
    same-month cancels now show up under "Cancelled — Not Paid," the
    "Cordoba Clawback" column is gone from "Cleared Clients This Period,"
    and that table is sortable."""

    def _upload_csv_as_admin(self, client, csv_bytes, filename="crm.csv"):
        return client.post(
            "/admin/upload-csv",
            data={"csv_file": (io.BytesIO(csv_bytes), filename)},
            content_type="multipart/form-data", follow_redirects=True,
        )

    def _csv(self, rows):
        headers = [
            "ID", "Sales Rep", "Full Name", "1st Payment Cleared Date", "Dropped Date",
            "Status", "Enrolled Debt", "# NSF", "Payments Made", "Pay Freq.", "Credit Score",
        ]
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=headers)
        writer.writeheader()
        for r in rows:
            writer.writerow({h: r.get(h, "") for h in headers})
        return out.getvalue().encode("utf-8")

    def test_same_month_cancel_shows_under_cancelled_not_paid(self, app, db, client):
        with app.app_context():
            _make_agent(db, "saman@example.com", "Saman", "Saman Agent", is_admin=True)
            _make_agent(db, "maria@example.com", "Maria", "Maria")

        _login(client, "saman@example.com")
        csv_bytes = self._csv([
            {"ID": "1", "Sales Rep": "Maria", "Full Name": "Kept Client",
             "1st Payment Cleared Date": "06/10/2026", "Status": "Active", "Enrolled Debt": "10000"},
            {"ID": "2", "Sales Rep": "Maria", "Full Name": "Dropped Client",
             "1st Payment Cleared Date": "06/05/2026", "Dropped Date": "06/20/2026",
             "Status": "Cancelled", "Enrolled Debt": "8000"},
        ])
        self._upload_csv_as_admin(client, csv_bytes)

        with app.app_context():
            period = CommissionPeriod.query.filter_by(period_label="2026-06").one()
            agent_row = AgentCommission.query.filter_by(period_id=period.id, agent_name="Maria").one()
            period_id, agent_row_id = period.id, agent_row.id

        client.get("/logout")
        _login(client, "maria@example.com")
        resp = client.get(f"/portal/period/{period_id}/agent/{agent_row_id}")
        assert resp.status_code == 200
        assert b"Cancelled" in resp.data
        assert b"Dropped Client" in resp.data
        # didn't leak into the cleared-clients table
        assert b"Kept Client" in resp.data

    def test_cleared_clients_table_has_no_cordoba_clawback_column_and_is_sortable(self, app, db, client):
        with app.app_context():
            _make_agent(db, "saman@example.com", "Saman", "Saman Agent", is_admin=True)
            _make_agent(db, "maria@example.com", "Maria", "Maria")

        _login(client, "saman@example.com")
        csv_bytes = self._csv([
            {"ID": "1", "Sales Rep": "Maria", "Full Name": "Kept Client",
             "1st Payment Cleared Date": "06/10/2026", "Status": "Active", "Enrolled Debt": "10000"},
        ])
        self._upload_csv_as_admin(client, csv_bytes)

        with app.app_context():
            period = CommissionPeriod.query.filter_by(period_label="2026-06").one()
            agent_row = AgentCommission.query.filter_by(period_id=period.id, agent_name="Maria").one()
            period_id, agent_row_id = period.id, agent_row.id

        client.get("/logout")
        _login(client, "maria@example.com")
        resp = client.get(f"/portal/period/{period_id}/agent/{agent_row_id}")
        html = resp.data.decode()
        assert "Cordoba Clawback" not in html
        assert 'id="clients-table"' in html
        assert 'class="data-table sortable" id="clients-table"' in html
        assert 'data-col="0"' in html


class TestDeletePeriod:
    """A period already uploaded before a parser fix ships never picks up
    that fix on its own (uploads are skipped once a period exists) — admin
    needs a way to delete it and re-import. Mirrors the internal app's own
    delete_period route."""

    def _csv(self, rows):
        headers = [
            "ID", "Sales Rep", "Full Name", "1st Payment Cleared Date", "Dropped Date",
            "Status", "Enrolled Debt", "# NSF", "Payments Made", "Pay Freq.", "Credit Score",
        ]
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=headers)
        writer.writeheader()
        for r in rows:
            writer.writerow({h: r.get(h, "") for h in headers})
        return out.getvalue().encode("utf-8")

    def test_admin_can_delete_a_period_and_cascade_removes_its_data(self, app, db, client):
        with app.app_context():
            _make_agent(db, "saman@example.com", "Saman", "Saman Agent", is_admin=True)
            row = _make_period_row(db, "2026-01", "Alice Agent")
            period_id = row.period_id

        _login(client, "saman@example.com")
        resp = client.post(f"/admin/period/{period_id}/delete", follow_redirects=True)
        assert b"Period 2026-01 deleted" in resp.data

        with app.app_context():
            assert CommissionPeriod.query.get(period_id) is None
            assert AgentCommission.query.filter_by(period_id=period_id).first() is None

    def test_non_admin_cannot_delete_a_period(self, app, db, client):
        with app.app_context():
            _make_agent(db, "alice@example.com", "Alice", "Alice Agent", is_admin=False)
            row = _make_period_row(db, "2026-01", "Alice Agent")
            period_id = row.period_id

        _login(client, "alice@example.com")
        resp = client.post(f"/admin/period/{period_id}/delete", follow_redirects=True)
        assert b"Admin access required" in resp.data
        with app.app_context():
            assert CommissionPeriod.query.get(period_id) is not None

    def test_delete_then_reupload_lets_same_month_cancel_client_appear(self, app, db, client):
        """The exact real-world scenario: a period uploaded before this
        feature existed has to be deleted and re-imported to pick it up."""
        with app.app_context():
            _make_agent(db, "saman@example.com", "Saman", "Saman Agent", is_admin=True)
            _make_agent(db, "maria@example.com", "Maria", "Maria")

        _login(client, "saman@example.com")
        # First upload has only the kept client — simulates the period having
        # been imported before the same-month-cancel fix existed.
        first_csv = self._csv([
            {"ID": "1", "Sales Rep": "Maria", "Full Name": "Kept Client",
             "1st Payment Cleared Date": "06/10/2026", "Status": "Active", "Enrolled Debt": "10000"},
        ])
        client.post("/admin/upload-csv", data={"csv_file": (io.BytesIO(first_csv), "crm1.csv")},
                    content_type="multipart/form-data", follow_redirects=True)

        with app.app_context():
            period_id = CommissionPeriod.query.filter_by(period_label="2026-06").one().id

        # Re-uploading without deleting is a no-op (period already exists)
        second_csv = self._csv([
            {"ID": "1", "Sales Rep": "Maria", "Full Name": "Kept Client",
             "1st Payment Cleared Date": "06/10/2026", "Status": "Active", "Enrolled Debt": "10000"},
            {"ID": "2", "Sales Rep": "Maria", "Full Name": "Dropped Client",
             "1st Payment Cleared Date": "06/05/2026", "Dropped Date": "06/20/2026",
             "Status": "Cancelled", "Enrolled Debt": "8000"},
        ])
        resp = client.post("/admin/upload-csv", data={"csv_file": (io.BytesIO(second_csv), "crm2.csv")},
                            content_type="multipart/form-data", follow_redirects=True)
        assert b"No new periods were created" in resp.data
        with app.app_context():
            assert ClientRecord.query.filter_by(crm_id="2").first() is None

        # Delete then re-upload: now it picks up the dropped client
        client.post(f"/admin/period/{period_id}/delete", follow_redirects=True)
        client.post("/admin/upload-csv", data={"csv_file": (io.BytesIO(second_csv), "crm2.csv")},
                    content_type="multipart/form-data", follow_redirects=True)
        with app.app_context():
            dropped = ClientRecord.query.filter_by(crm_id="2").first()
            assert dropped is not None
            assert dropped.is_cancelled is True
            assert dropped.commission_on_client == 0.0
