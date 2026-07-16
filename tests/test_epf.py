"""EPF tab behavior (owner decision, July 2026): DISPLAY ONLY.

EPF rows are matched by Contact ID to our ClientRecord history to find the sales
rep, placed in the month of the EPF tab's Cleared Date, and shown in an "EPF"
section on that agent's page. They must NEVER change units, tier, or commission,
and a client already commissioned is never shown (no hint of paying twice)."""

import io
from types import SimpleNamespace

import openpyxl
import pytest

from app.cordoba_parser import parse_cordoba_payout
from app.models import CommissionPeriod, AgentCommission, ClientRecord, EpfClient
from app.routes import _apply_epf_rows

FAKE_FILE = SimpleNamespace(filename="cordoba_payouts.xlsx")


def build_cordoba_xlsx(epf_rows):
    """Minimal workbook with the three expected tabs; EPF rows are (contact_id, name, cleared_date)."""
    wb = openpyxl.Workbook()
    first = wb.active
    first.title = "First Pays"
    first.append(["ID", "Full Name"])
    epf = wb.create_sheet("EPF")
    epf.append(["Contact ID", "Full Name", "Cleared Date"])
    for row in epf_rows:
        epf.append(list(row))
    cb = wb.create_sheet("Chargebacks")
    cb.append(["ID", "Full Name", "Dropped Date"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def seed_client(db, crm_id="7001", agent="Maria", is_cleared=False):
    period = CommissionPeriod(period_label="2026-05", filename="crm.csv", total_agents=1)
    db.session.add(period)
    db.session.flush()
    agent_row = AgentCommission(
        period_id=period.id, agent_name=agent,
        units_cleared=1, total_cleared_debt=10_000.0, cancellation_rate=0.0,
        hourly_draw=0.0, raw_tier=1, adjusted_tier=1, tier_rate=0.01,
        gross_commission=100.0, clawback_amount=0.0, net_commission=100.0,
        payout=100.0, payout_type="commission", source="crm", notes="",
    )
    db.session.add(agent_row)
    db.session.flush()
    db.session.add(ClientRecord(
        period_id=period.id, agent_commission_id=agent_row.id,
        crm_id=crm_id, agent_name=agent, client_name="Jane Roe",
        enrolled_debt=10_000.0, is_cleared=is_cleared, is_pending=not is_cleared,
        status="Pending Affiliate Cancellation" if not is_cleared else "Active",
    ))
    db.session.commit()
    return agent_row


def test_parser_extracts_epf_rows_with_cleared_date():
    data = build_cordoba_xlsx([("7001", "Jane Roe", "06/15/2026")])
    parsed = parse_cordoba_payout(data)
    assert parsed["errors"] == []
    assert parsed["epf_rows"] == [
        {"crm_id": "7001", "client_name": "Jane Roe", "cleared_date": "06/15/2026"},
    ]
    # EPF still confirms Cordoba paid us (feeds the CordobaPaidClient ledger)
    assert {"crm_id": "7001", "client_name": "Jane Roe", "source": "epf"} in parsed["paid_ids"]


def test_epf_row_stored_for_matched_agent_and_month(db):
    seed_client(db, is_cleared=False)
    added, skipped, unmatched, missing = _apply_epf_rows(
        FAKE_FILE, {"epf_rows": [{"crm_id": "7001", "client_name": "Jane Roe",
                                  "cleared_date": "06/15/2026"}]})
    db.session.commit()
    assert (added, skipped, unmatched, missing) == (1, 0, 0, 0)
    entry = EpfClient.query.one()
    assert entry.agent_name == "Maria"
    assert entry.period_label == "2026-06"      # from the EPF Cleared Date
    assert entry.cleared_date == "06/15/2026"


def test_epf_never_changes_commission(db):
    agent_row = seed_client(db, is_cleared=False)
    before = (agent_row.units_cleared, agent_row.gross_commission, agent_row.net_commission)
    _apply_epf_rows(FAKE_FILE, {"epf_rows": [{"crm_id": "7001", "client_name": "Jane Roe",
                                              "cleared_date": "06/15/2026"}]})
    db.session.commit()
    refreshed = db.session.get(AgentCommission, agent_row.id)
    assert (refreshed.units_cleared, refreshed.gross_commission, refreshed.net_commission) == before
    # and no new period/agent rows were created just for display
    assert CommissionPeriod.query.count() == 1
    assert AgentCommission.query.count() == 1


def test_already_commissioned_client_is_skipped(db):
    seed_client(db, is_cleared=True)
    added, skipped, unmatched, missing = _apply_epf_rows(
        FAKE_FILE, {"epf_rows": [{"crm_id": "7001", "client_name": "Jane Roe",
                                  "cleared_date": "06/15/2026"}]})
    assert (added, skipped, unmatched, missing) == (0, 1, 0, 0)
    assert EpfClient.query.count() == 0


def test_unknown_contact_id_is_skipped(db):
    seed_client(db)
    added, skipped, unmatched, missing = _apply_epf_rows(
        FAKE_FILE, {"epf_rows": [{"crm_id": "9999", "client_name": "Nobody",
                                  "cleared_date": "06/15/2026"}]})
    assert (added, skipped, unmatched, missing) == (0, 0, 1, 0)


def test_missing_cleared_date_is_skipped(db):
    seed_client(db)
    added, skipped, unmatched, missing = _apply_epf_rows(
        FAKE_FILE, {"epf_rows": [{"crm_id": "7001", "client_name": "Jane Roe",
                                  "cleared_date": None}]})
    assert (added, skipped, unmatched, missing) == (0, 0, 0, 1)


def test_reupload_is_a_noop(db):
    seed_client(db)
    row = {"epf_rows": [{"crm_id": "7001", "client_name": "Jane Roe",
                         "cleared_date": "06/15/2026"}]}
    _apply_epf_rows(FAKE_FILE, row)
    db.session.commit()
    added, *_ = _apply_epf_rows(FAKE_FILE, row)
    assert added == 0
    assert EpfClient.query.count() == 1
