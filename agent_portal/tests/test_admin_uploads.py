"""Tests for the admin dashboard's "recent uploads" lists and their
delete/reset actions — lets a wrong upload (CRM file, Cordoba payout file, or
commission-history ledger) be fully undone without shelling into the DB.
CRM export and history-backfill deletes just reuse the existing well-tested
period-cascade delete, grouped by filename. The Cordoba delete is the
sensitive one: it has to reverse real clawback dollars, not just remove
ledger rows, so it gets the most coverage here."""

from types import SimpleNamespace

import pytest

from agent_portal.cordoba_ingest import _apply_cordoba_chargebacks, delete_cordoba_upload, list_cordoba_uploads
from agent_portal.ingest import delete_periods_by_filename, group_periods_by_filename
from agent_portal.models import (
    Agent, AgentAlias, AgentCommission, ClientRecord, CommissionPeriod,
    CordobaChargebackEntry, CordobaChargebackMatchedClient,
    CordobaChargedBackClient, CordobaPaidClient,
)

FAKE_FILE = SimpleNamespace(filename="cordoba_payouts.xlsx")
CRM_ID = "4478112"


def _make_agent(db, email, display_name, agent_name, is_admin=False, password="pw12345"):
    agent = Agent(email=email, display_name=display_name, is_admin=is_admin)
    agent.set_password(password)
    db.session.add(agent)
    db.session.flush()
    db.session.add(AgentAlias(agent_id=agent.id, agent_name=agent_name))
    db.session.commit()
    return agent


def _login(client, email, password="pw12345"):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=True)


def chargeback_row(crm_id=CRM_ID, name="John Doe"):
    return {"crm_id": crm_id, "client_name": name}


def parsed(rows):
    return {"paid_ids": [], "chargebacks": rows, "errors": []}


def seed_paid_june_client(db, filename=FAKE_FILE.filename):
    """June 2026: agent Maria, 25 units, $500k -> Tier 2, $6,250 gross.
    One of those clients is John Doe (CRM_ID), $30k debt, confirmed paid by
    a Cordoba file tagged `filename`."""
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
    db.session.add(CordobaPaidClient(
        crm_id=CRM_ID, client_name="John Doe", source="first_pays", uploaded_filename=filename,
    ))
    db.session.commit()
    return period, agent


class TestDeleteCordobaUpload:
    def test_reverses_a_clawback_and_removes_the_holding_period(self, db):
        seed_paid_june_client(db)
        _apply_cordoba_chargebacks(FAKE_FILE, parsed([chargeback_row()]))
        db.session.commit()
        aug = CommissionPeriod.query.filter_by(period_label="2026-08").one()
        assert AgentCommission.query.filter_by(period_id=aug.id).count() == 1

        result = delete_cordoba_upload(FAKE_FILE.filename)

        assert result["clawbacks_reversed"] == 1
        assert result["amount_reversed"] == pytest.approx(375.0)
        assert CommissionPeriod.query.filter_by(period_label="2026-08").first() is None
        assert CordobaChargedBackClient.query.count() == 0
        assert ClientRecord.query.filter_by(crm_id=CRM_ID, clawback_applied=True).count() == 0

    def test_does_not_touch_a_real_period_with_other_agents(self, db):
        """If the dropped-month period already had OTHER real commission data,
        reversing the clawback must only remove its own footprint, not the
        whole period."""
        seed_paid_june_client(db)
        _apply_cordoba_chargebacks(FAKE_FILE, parsed([chargeback_row()]))
        db.session.commit()
        aug = CommissionPeriod.query.filter_by(period_label="2026-08").one()
        db.session.add(AgentCommission(
            period_id=aug.id, agent_name="Bob",
            units_cleared=10, total_cleared_debt=100_000.0, cancellation_rate=0.0,
            hourly_draw=0.0, raw_tier=1, adjusted_tier=1, tier_rate=0.01,
            gross_commission=1_000.0, clawback_amount=0.0, net_commission=1_000.0,
            payout=1_000.0, payout_type="commission", source="crm", notes="",
        ))
        db.session.commit()

        delete_cordoba_upload(FAKE_FILE.filename)

        assert CommissionPeriod.query.filter_by(period_label="2026-08").first() is not None
        assert AgentCommission.query.filter_by(period_id=aug.id, agent_name="Maria").first() is None
        assert AgentCommission.query.filter_by(period_id=aug.id, agent_name="Bob").first() is not None

    def test_unflags_cordoba_paid_only_if_no_other_file_confirmed_it(self, db):
        seed_paid_june_client(db)
        ClientRecord.query.filter_by(crm_id=CRM_ID).update({"cordoba_paid": True})
        db.session.commit()

        result = delete_cordoba_upload(FAKE_FILE.filename)

        assert result["cordoba_paid_unflagged"] == 1
        assert ClientRecord.query.filter_by(crm_id=CRM_ID).first().cordoba_paid is False

    def test_does_not_unflag_a_different_clients_cordoba_paid_status(self, db):
        """CordobaPaidClient.crm_id is globally unique — only the first file to
        confirm a given client ever gets a row, so there's no scenario where two
        files both confirm the SAME crm_id. This just confirms the unflag is
        correctly scoped to this file's own crm_ids, not every confirmed client."""
        seed_paid_june_client(db)
        db.session.add(CordobaPaidClient(
            crm_id="9999999", client_name="Other Client", source="epf", uploaded_filename="other_file.xlsx",
        ))
        ClientRecord.query.filter_by(crm_id=CRM_ID).update({"cordoba_paid": True})
        db.session.add(ClientRecord(
            period_id=CommissionPeriod.query.first().id, crm_id="9999999", agent_name="Maria",
            client_name="Other Client", enrolled_debt=1_000.0, is_cleared=True, cordoba_paid=True,
        ))
        db.session.commit()

        delete_cordoba_upload(FAKE_FILE.filename)

        assert ClientRecord.query.filter_by(crm_id="9999999").first().cordoba_paid is True

    def test_clears_display_only_ledgers(self, db):
        seed_paid_june_client(db)
        db.session.add(CordobaChargebackMatchedClient(
            crm_id=CRM_ID, client_name="John Doe", uploaded_filename=FAKE_FILE.filename,
        ))
        db.session.add(CordobaChargebackEntry(
            crm_id=CRM_ID, agent_name="Maria", period_label="2026-08", uploaded_filename=FAKE_FILE.filename,
        ))
        db.session.commit()

        result = delete_cordoba_upload(FAKE_FILE.filename)

        assert (result["matched_removed"], result["entries_removed"]) == (1, 1)
        assert CordobaChargebackMatchedClient.query.count() == 0
        assert CordobaChargebackEntry.query.count() == 0

    def test_unrelated_files_untouched(self, db):
        """Deleting one file's upload must not touch another file's ledger rows."""
        seed_paid_june_client(db, filename="file_a.xlsx")
        db.session.add(CordobaPaidClient(
            crm_id="9999999", client_name="Other Client", source="epf", uploaded_filename="file_b.xlsx",
        ))
        db.session.commit()

        delete_cordoba_upload("file_a.xlsx")

        assert CordobaPaidClient.query.filter_by(uploaded_filename="file_b.xlsx").count() == 1


class TestListCordobaUploads:
    def test_groups_by_filename_with_counts(self, db):
        seed_paid_june_client(db)
        _apply_cordoba_chargebacks(FAKE_FILE, parsed([chargeback_row()]))
        db.session.commit()

        uploads = list_cordoba_uploads()

        assert len(uploads) == 1
        assert uploads[0]["filename"] == FAKE_FILE.filename
        assert uploads[0]["paid_count"] == 1
        assert uploads[0]["clawback_count"] == 1
        assert uploads[0]["clawback_total"] == pytest.approx(375.0)


class TestGroupAndDeletePeriodsByFilename:
    def _make_period(self, db, label, filename, source="drive"):
        period = CommissionPeriod(period_label=label, filename=filename, total_agents=1)
        db.session.add(period)
        db.session.flush()
        db.session.add(AgentCommission(
            period_id=period.id, agent_name="Agent",
            units_cleared=5, total_cleared_debt=50_000.0, cancellation_rate=0.0,
            hourly_draw=0.0, raw_tier=1, adjusted_tier=1, tier_rate=0.01,
            gross_commission=500.0, clawback_amount=0.0, net_commission=500.0,
            payout=500.0, payout_type="commission", source=source, notes="",
        ))
        db.session.commit()
        return period

    def test_group_periods_by_filename_groups_multi_month_upload(self, db):
        self._make_period(db, "2026-01", "crm_export.csv")
        self._make_period(db, "2026-02", "crm_export.csv")
        self._make_period(db, "2025-12", "other_file.csv")

        groups = group_periods_by_filename(CommissionPeriod.query.all())
        by_name = {g["filename"]: g for g in groups}

        assert {p.period_label for p in by_name["crm_export.csv"]["periods"]} == {"2026-01", "2026-02"}
        assert {p.period_label for p in by_name["other_file.csv"]["periods"]} == {"2025-12"}

    def test_delete_periods_by_filename_only_deletes_matching_source(self, db):
        self._make_period(db, "2026-01", "crm_export.csv", source="drive")
        self._make_period(db, "2025-06", "crm_export.csv", source="history_import")

        deleted = delete_periods_by_filename("crm_export.csv", ("drive", "manual"))

        assert deleted == ["2026-01"]
        assert CommissionPeriod.query.filter_by(period_label="2026-01").first() is None
        assert CommissionPeriod.query.filter_by(period_label="2025-06").first() is not None


class TestAdminUploadRoutes:
    def test_delete_crm_upload_route(self, app, db, client):
        with app.app_context():
            _make_agent(db, "saman@example.com", "Saman", "Saman Agent", is_admin=True)
            TestGroupAndDeletePeriodsByFilename()._make_period(db, "2026-01", "crm_export.csv")

        _login(client, "saman@example.com")
        resp = client.post(
            "/admin/uploads/crm/delete", data={"filename": "crm_export.csv"}, follow_redirects=True,
        )
        assert b"Deleted 1 period" in resp.data
        with app.app_context():
            assert CommissionPeriod.query.filter_by(period_label="2026-01").first() is None

    def test_delete_cordoba_upload_route(self, app, db, client):
        with app.app_context():
            _make_agent(db, "saman@example.com", "Saman", "Saman Agent", is_admin=True)
            seed_paid_june_client(db)
            _apply_cordoba_chargebacks(FAKE_FILE, parsed([chargeback_row()]))
            db.session.commit()

        _login(client, "saman@example.com")
        resp = client.post(
            "/admin/uploads/cordoba/delete", data={"filename": FAKE_FILE.filename}, follow_redirects=True,
        )
        assert b"Reversed 1 clawback" in resp.data
        with app.app_context():
            assert CommissionPeriod.query.filter_by(period_label="2026-08").first() is None

    def test_non_admin_cannot_delete_uploads(self, app, db, client):
        with app.app_context():
            _make_agent(db, "alice@example.com", "Alice", "Alice Agent", is_admin=False)
            TestGroupAndDeletePeriodsByFilename()._make_period(db, "2026-01", "crm_export.csv")

        _login(client, "alice@example.com")
        resp = client.post(
            "/admin/uploads/crm/delete", data={"filename": "crm_export.csv"}, follow_redirects=True,
        )
        assert b"Admin access required" in resp.data
        with app.app_context():
            assert CommissionPeriod.query.filter_by(period_label="2026-01").first() is not None
