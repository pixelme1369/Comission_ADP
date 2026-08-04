"""Backfills past commission history from a prior account manager's ledger
(.xlsx or .csv, NOT a CRM export). Recreates real CommissionPeriod /
AgentCommission / ClientRecord rows for those months — same shape the CRM
flow produces — so this history is indistinguishable from a real upload for
Cordoba chargeback matching (cordoba_ingest.py looks up
ClientRecord.is_cleared=True by crm_id, regardless of which upload flow
created it). Ported from app/routes.py's _save_commission_history_period and
upload_commission_history handler."""

from agent_portal import db
from commission_core.commission_history_parser import parse_commission_history
from agent_portal.ingest import _new_client_record
from agent_portal.models import AgentCommission, ClientRecord, CommissionPeriod, CordobaPaidClient

ALLOWED_HISTORY_EXTENSIONS = {"xlsx", "csv"}


def allowed_history_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_HISTORY_EXTENSIONS


def _save_commission_history_period(period_label, results, filename, already_cordoba_paid_ids):
    period = CommissionPeriod(period_label=period_label, filename=filename, total_agents=len(results))
    db.session.add(period)
    db.session.flush()

    for r in results:
        cleared_clients = r.pop("_cleared_clients", [])
        clawback_clients = r.pop("_clawback_clients", [])

        agent_obj = AgentCommission(period_id=period.id, **r)
        db.session.add(agent_obj)
        db.session.flush()

        for cr in cleared_clients:
            db.session.add(_new_client_record(
                period.id, agent_obj.id, cr,
                is_cleared=True,
                is_pending=False,
                is_cancelled=False,
                commission_on_client=round(cr.get("enrolled_debt", 0.0) * agent_obj.tier_rate, 2),
                cordoba_paid=cr.get("crm_id") in already_cordoba_paid_ids,
            ))

        for cr in clawback_clients:
            db.session.add(_new_client_record(
                period.id, agent_obj.id, cr,
                is_cleared=False,
                is_pending=False,
                is_cancelled=True,
                commission_on_client=0.0,
                clawback_applied=True,
                clawback_period_id=period.id,
                clawback_amount=cr.get("clawback_amount", 0.0),
            ))

    return period


def import_commission_history_files(files, year):
    """Parses and saves one or more history-ledger files for the given year.
    Commits. Returns {"saved_period_ids": [...], "periods_skipped": int, "warnings": [...]}."""
    already_cordoba_paid_ids = {p.crm_id for p in CordobaPaidClient.query.all()}

    saved_period_ids = []
    periods_skipped = 0
    warnings = []

    for file in files:
        file_bytes = file.read()
        parsed = parse_commission_history(file_bytes, file.filename, year)

        for err in parsed["errors"]:
            warnings.append(f"{file.filename}: {err}")

        for period_data in parsed["periods"]:
            period_label = period_data["period_label"]
            existing = CommissionPeriod.query.filter_by(period_label=period_label).first()
            if existing:
                warnings.append(
                    f"Period {period_label} already exists (uploaded "
                    f"{existing.uploaded_at.strftime('%Y-%m-%d')}). Delete it first before "
                    "re-importing history for that month."
                )
                periods_skipped += 1
                continue

            period = _save_commission_history_period(
                period_label, period_data["results"], file.filename, already_cordoba_paid_ids
            )
            saved_period_ids.append(period.id)

        db.session.commit()

    return {"saved_period_ids": saved_period_ids, "periods_skipped": periods_skipped, "warnings": warnings}
