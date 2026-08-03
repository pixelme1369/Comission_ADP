"""Shared CRM-import persistence logic used by both drive_sync.py (automated
daily Drive sync) and the admin manual-CSV-upload fallback, so the two entry
points can never drift on how a parsed CRM export gets saved to the DB.
Mirrors app/routes.py's upload_crm handler and _new_client_record helper."""

from agent_portal import db
from agent_portal.models import (
    AgentCommission, ClientRecord, CommissionPeriod,
    CordobaChargedBackClient, CordobaPaidClient,
)


def _new_client_record(period_id, agent_commission_id, cr, **overrides):
    fields = dict(
        period_id=period_id,
        agent_commission_id=agent_commission_id,
        crm_id=cr.get("crm_id"),
        agent_name=cr["agent_name"],
        client_name=cr.get("client_name"),
        email=cr.get("email"),
        phone=cr.get("phone"),
        stage=cr.get("stage"),
        status=cr.get("status"),
        submitted_date=cr.get("submitted_date"),
        enrolled_date=cr.get("enrolled_date"),
        first_payment_date=cr.get("first_payment_date"),
        first_payment_cleared_date=cr.get("first_payment_cleared_date"),
        second_payment_cleared_date=cr.get("second_payment_cleared_date"),
        dropped_date=cr.get("dropped_date"),
        pay_freq=cr.get("pay_freq"),
        payments_made=cr.get("payments_made", 0),
        nsf_count=cr.get("nsf_count", 0),
        enrolled_debt=cr.get("enrolled_debt", 0.0),
        credit_score=cr.get("credit_score"),
        is_low_credit=cr.get("is_low_credit", False),
        is_cleared=cr.get("is_cleared", False),
        is_pending=cr.get("is_pending", False),
        is_cancelled=cr.get("is_cancelled", False),
        commission_on_client=cr.get("commission_on_client", 0.0),
    )
    fields.update(overrides)
    return ClientRecord(**fields)


def already_known_crm_id_sets():
    """crm_ids this DB already knows about, for the parser's late-activation /
    clawback-guard / low-credit-guard logic. already_charged_back comes from
    the Cordoba chargeback ledger (see cordoba_ingest.py) so a CRM upload
    reflecting a drop already clawed back via a Cordoba Chargebacks file never
    double-charges the agent."""
    already_cleared = {
        r[0] for r in db.session.query(ClientRecord.crm_id)
        .filter(ClientRecord.is_cleared.is_(True)) if r[0]
    }
    already_charged_back = {
        r[0] for r in db.session.query(CordobaChargedBackClient.crm_id) if r[0]
    }
    already_low_credit = {
        r[0] for r in db.session.query(ClientRecord.crm_id)
        .filter(ClientRecord.is_low_credit.is_(True)) if r[0]
    }
    return already_cleared, already_charged_back, already_low_credit


def save_period_results(period_results, filename, source_label="drive"):
    """Persist parse_crm_and_calculate()'s output. Does NOT commit — caller
    commits (so drive_sync.py can add its SyncedFile ledger row in the same
    transaction). Returns {"periods_created": [...], "warnings": [...]}."""
    periods_created = []
    warnings = []
    seen_errors = set()

    for parsed in period_results:
        for err in parsed.get("errors", []):
            if err not in seen_errors:
                warnings.append(err)
                seen_errors.add(err)

        if not parsed["results"] or not parsed["period_label"]:
            continue

        period_label = parsed["period_label"]
        existing = CommissionPeriod.query.filter_by(period_label=period_label).first()
        if existing:
            # A skipped period silently discards anything the parser computed for
            # it, including any NEW clawback routed there — warn rather than lose
            # it silently (mirrors app/routes.py's upload_crm skip-warning).
            cb_clients = [c for r in parsed["results"] for c in r.get("_clawback_clients", [])]
            if cb_clients:
                cb_ids = {c["crm_id"] for c in cb_clients if c.get("crm_id")}
                already_recorded = {
                    r[0] for r in db.session.query(ClientRecord.crm_id).filter(
                        ClientRecord.crm_id.in_(cb_ids),
                        ClientRecord.clawback_applied.is_(True),
                    )
                } if cb_ids else set()
                new_cb = [c for c in cb_clients
                          if not c.get("crm_id") or c["crm_id"] not in already_recorded]
                if new_cb:
                    total = sum(c.get("clawback_amount", 0.0) for c in new_cb)
                    warnings.append(
                        f"Period {period_label} already exists — {len(new_cb)} new clawback(s) "
                        f"totaling ${total:,.2f} were NOT applied. Delete period {period_label} "
                        "and re-import to apply them."
                    )
            continue

        period = CommissionPeriod(
            period_label=period_label, filename=filename, total_agents=len(parsed["results"]),
        )
        db.session.add(period)
        db.session.flush()

        # A client already confirmed paid via a prior Cordoba First Pays/EPF
        # upload should come in pre-flagged even though this file predates it.
        already_cordoba_paid_ids = {r[0] for r in db.session.query(CordobaPaidClient.crm_id)}

        agent_obj_map = {}
        for r in parsed["results"]:
            all_period_clients = r.pop("_all_period_clients", [])
            clawback_clients = r.pop("_clawback_clients", [])
            r.pop("_cleared_clients", None)
            r.pop("_period_label", None)
            r["source"] = source_label

            agent_obj = AgentCommission(period_id=period.id, **r)
            db.session.add(agent_obj)
            db.session.flush()
            agent_obj_map[r["agent_name"]] = {
                "obj": agent_obj,
                "all_period_clients": all_period_clients,
                "clawback_clients": clawback_clients,
            }

        for agent_name, data in agent_obj_map.items():
            agent_obj = data["obj"]
            for cr in data["all_period_clients"]:
                db.session.add(_new_client_record(
                    period.id, agent_obj.id, cr,
                    is_late_activation=cr.get("is_late_activation", False),
                    original_cleared_period=cr.get("original_cleared_period"),
                    cordoba_paid=cr.get("crm_id") in already_cordoba_paid_ids,
                ))
            for cr in data["clawback_clients"]:
                db.session.add(_new_client_record(
                    period.id, agent_obj.id, cr,
                    is_cleared=False, is_pending=False, is_cancelled=True,
                    commission_on_client=0.0, clawback_applied=True,
                    clawback_period_id=period.id, clawback_amount=cr.get("clawback_amount", 0.0),
                ))

        periods_created.append(period_label)

    return {"periods_created": periods_created, "warnings": warnings}


def group_periods_by_filename(periods):
    """Groups CommissionPeriod rows by their source filename (a multi-month CRM
    export or history-ledger upload can create several periods from one file),
    for the admin dashboard's per-upload-type "recent uploads" lists — newest
    upload first."""
    groups = {}
    for p in periods:
        groups.setdefault(p.filename or "(unknown)", []).append(p)
    return sorted(
        ({"filename": name, "periods": sorted(rows, key=lambda r: r.period_label)}
         for name, rows in groups.items()),
        key=lambda g: max(r.uploaded_at for r in g["periods"]), reverse=True,
    )


def delete_periods_by_filename(filename, source_values):
    """Deletes every CommissionPeriod with this filename whose agents were
    created by one of the given upload types (source_values, e.g. ("drive",
    "manual") for a CRM upload or ("history_import",) for a history backfill)
    — lets the admin dashboard delete/reset an entire multi-month upload in
    one action instead of one period at a time. Cascades to AgentCommission/
    ClientRecord via the model relationships, same as deleting a period
    individually. Returns the list of period_labels actually deleted."""
    periods = CommissionPeriod.query.filter_by(filename=filename).all()
    deleted_labels = []
    for period in periods:
        if period.agents and period.agents[0].source in source_values:
            deleted_labels.append(period.period_label)
            db.session.delete(period)
    db.session.commit()
    return deleted_labels
