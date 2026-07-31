"""Applies a parsed Cordoba payout file (see cordoba_parser.py) to the DB:
First Pays/EPF tabs flag ClientRecord.cordoba_paid = True (informational),
the Chargebacks tab triggers actual agent commission clawbacks. Ported from
app/routes.py's _apply_cordoba_paid_flags / _mark_cordoba_chargeback_matches /
_apply_cordoba_chargebacks / _list_cordoba_chargebacks / _process_cordoba_file
— same logic, same gates, adapted to this app's models/imports."""

from agent_portal import db
from agent_portal.calculator import calculate_clawback_amount, get_fixed_rate
from agent_portal.cordoba_parser import parse_cordoba_payout
from agent_portal.crm_parser import _parse_date, _period_of
from agent_portal.models import (
    AgentCommission, ClientRecord, CommissionPeriod,
    CordobaChargebackEntry, CordobaChargebackMatchedClient,
    CordobaChargedBackClient, CordobaPaidClient,
)


def _client_label(client_name, crm_id):
    return f"{client_name} (ID {crm_id})" if client_name else crm_id


def _get_or_create_agent_period_row(period_label, agent_name, filename):
    """Find (or create a zero-unit) AgentCommission row to carry a clawback that
    has no cleared units of its own in this period."""
    period = CommissionPeriod.query.filter_by(period_label=period_label).first()
    if not period:
        period = CommissionPeriod(period_label=period_label, filename=filename, total_agents=0)
        db.session.add(period)
        db.session.flush()

    agent_row = AgentCommission.query.filter_by(period_id=period.id, agent_name=agent_name).first()
    if not agent_row:
        agent_row = AgentCommission(
            period_id=period.id, agent_name=agent_name,
            units_cleared=0, total_cleared_debt=0.0, cancellation_rate=0.0, hourly_draw=0.0,
            raw_tier=0, adjusted_tier=0, tier_rate=0.0, gross_commission=0.0,
            clawback_amount=0.0, net_commission=0.0, payout=0.0, payout_type="none",
            quality_bonus_eligible=False, cancellation_penalty_applied=False, nsf_flagged=False,
            pending_units=0, pending_debt=0.0, source="cordoba", notes="",
        )
        db.session.add(agent_row)
        db.session.flush()
        period.total_agents = (period.total_agents or 0) + 1

    return period, agent_row


def _apply_cordoba_paid_flags(file, parsed):
    """Check OUR existing ClientRecord IDs against Cordoba's First Pays/EPF tabs —
    any match flips cordoba_paid = True, remembered forever in CordobaPaidClient."""
    incoming_ids = {row["crm_id"] for row in parsed["paid_ids"] if row["crm_id"]}
    if not incoming_ids:
        return 0, 0

    already_known_ids = {
        p.crm_id for p in CordobaPaidClient.query.filter(CordobaPaidClient.crm_id.in_(incoming_ids)).all()
    }

    new_count = 0
    seen_this_file = set()
    for row in parsed["paid_ids"]:
        crm_id = row["crm_id"]
        if not crm_id or crm_id in already_known_ids or crm_id in seen_this_file:
            continue
        seen_this_file.add(crm_id)
        db.session.add(CordobaPaidClient(
            crm_id=crm_id, client_name=row.get("client_name"), source=row["source"],
            uploaded_filename=file.filename,
        ))
        new_count += 1

    # Unconditional update (never filter on cordoba_paid.is_(False) — see CLAUDE.md /
    # app/routes.py's own note on why a NULL-vs-False gap makes that filter unsafe).
    flipped = ClientRecord.query.filter(ClientRecord.crm_id.in_(incoming_ids)).update(
        {"cordoba_paid": True}, synchronize_session=False
    )

    return new_count, flipped


def _mark_cordoba_chargeback_matches(file, parsed):
    """Display-only companion to _apply_cordoba_chargebacks: for every ID in the
    Chargebacks tab, check it against ALL of our own commission reports (any
    period, any status). Any match is recorded forever and drives the per-client
    "Cordoba Clawback" Yes/No badge, independent of whether the real deduction
    below could be applied yet."""
    chargebacks = parsed.get("chargebacks", [])
    incoming_ids = {row["crm_id"] for row in chargebacks if row["crm_id"]}
    if not incoming_ids:
        return 0, []

    already_marked = {
        r[0] for r in db.session.query(CordobaChargebackMatchedClient.crm_id)
        .filter(CordobaChargebackMatchedClient.crm_id.in_(incoming_ids))
    }
    known_ids = {
        r[0] for r in db.session.query(ClientRecord.crm_id)
        .filter(ClientRecord.crm_id.in_(incoming_ids))
    }

    marked = 0
    unmatched = []
    seen_this_file = set()
    for row in chargebacks:
        crm_id = row["crm_id"]
        if not crm_id or crm_id in seen_this_file:
            continue
        seen_this_file.add(crm_id)

        if crm_id not in known_ids:
            unmatched.append(_client_label(row.get("client_name"), crm_id))
            continue
        if crm_id in already_marked:
            continue

        db.session.add(CordobaChargebackMatchedClient(
            crm_id=crm_id, client_name=row.get("client_name"), uploaded_filename=file.filename,
        ))
        marked += 1

    return marked, unmatched


def _apply_cordoba_chargebacks(file, parsed):
    """Cross-reference the Chargebacks tab against OUR OWN ClientRecord history.
    Claws back unconditionally (no safe-payment-threshold check) once every gate
    passes: we paid the agent, Cordoba confirmed paying us, not already clawed
    back elsewhere, and we have our own dropped_date to place the deduction."""
    chargebacks = parsed.get("chargebacks", [])
    incoming_ids = {row["crm_id"] for row in chargebacks if row["crm_id"]}
    if not incoming_ids:
        return 0, 0.0, [], [], [], []

    already_charged_back = {
        c.crm_id for c in
        CordobaChargedBackClient.query.filter(CordobaChargedBackClient.crm_id.in_(incoming_ids)).all()
    }
    confirmed_paid_ids = {
        p.crm_id for p in
        CordobaPaidClient.query.filter(CordobaPaidClient.crm_id.in_(incoming_ids)).all()
    }
    already_clawed_elsewhere = {
        r[0] for r in
        db.session.query(ClientRecord.crm_id).filter(
            ClientRecord.crm_id.in_(incoming_ids),
            ClientRecord.clawback_applied.is_(True),
        )
    }

    applied_count = 0
    total_clawed_back = 0.0
    skipped_not_commissioned = []
    skipped_not_confirmed_paid = []
    skipped_already_clawed = []
    skipped_no_dropped_date = []
    seen_this_file = set()

    for row in chargebacks:
        crm_id = row["crm_id"]
        if not crm_id or crm_id in already_charged_back or crm_id in seen_this_file:
            continue
        seen_this_file.add(crm_id)

        if crm_id in already_clawed_elsewhere:
            skipped_already_clawed.append(_client_label(row.get("client_name"), crm_id))
            continue

        client_rec = (
            ClientRecord.query.filter_by(crm_id=crm_id, is_cleared=True)
            .order_by(ClientRecord.id.desc()).first()
        )
        if not client_rec:
            skipped_not_commissioned.append(_client_label(row.get("client_name"), crm_id))
            continue

        if crm_id not in confirmed_paid_ids:
            skipped_not_confirmed_paid.append(_client_label(row.get("client_name"), crm_id))
            continue

        dropped_dt = _parse_date(client_rec.dropped_date or "")
        dropped_period = _period_of(dropped_dt)
        if not dropped_period:
            skipped_no_dropped_date.append(_client_label(row.get("client_name"), crm_id))
            continue

        agent_name = client_rec.agent_name
        client_debt = client_rec.enrolled_debt or 0.0
        orig_agent_row = client_rec.agent_commission

        if orig_agent_row and orig_agent_row.units_cleared > 0:
            cb = calculate_clawback_amount(
                orig_agent_row.units_cleared,
                orig_agent_row.total_cleared_debt,
                orig_agent_row.gross_commission,
                orig_agent_row.cancellation_rate,
                client_debt,
                agent_name=agent_name,
            )
        else:
            cb = round(client_debt * (get_fixed_rate(agent_name) or 0.01), 2)

        if cb <= 0:
            continue

        period, agent_row = _get_or_create_agent_period_row(dropped_period, agent_name, file.filename)

        agent_row.clawback_amount = round((agent_row.clawback_amount or 0.0) + cb, 2)
        agent_row.net_commission = max(0.0, round(agent_row.gross_commission - agent_row.clawback_amount, 2))
        note = f"Cordoba chargeback: -${cb:,.2f} for {client_rec.client_name or crm_id} (ID {crm_id})"
        agent_row.notes = f"{agent_row.notes} | {note}" if agent_row.notes else note

        db.session.add(ClientRecord(
            period_id=period.id,
            agent_commission_id=agent_row.id,
            crm_id=crm_id,
            agent_name=agent_name,
            client_name=client_rec.client_name,
            email=client_rec.email,
            phone=client_rec.phone,
            stage=client_rec.stage,
            status="Cordoba Chargeback",
            enrolled_date=client_rec.enrolled_date,
            first_payment_cleared_date=client_rec.first_payment_cleared_date,
            dropped_date=client_rec.dropped_date,
            pay_freq=client_rec.pay_freq,
            payments_made=client_rec.payments_made,
            enrolled_debt=client_debt,
            is_cleared=False,
            is_pending=False,
            is_cancelled=True,
            commission_on_client=0.0,
            clawback_applied=True,
            clawback_period_id=period.id,
            clawback_amount=cb,
        ))

        db.session.add(CordobaChargedBackClient(
            crm_id=crm_id, client_name=client_rec.client_name, agent_name=agent_name,
            clawback_amount=cb, dropped_period=dropped_period, uploaded_filename=file.filename,
        ))

        applied_count += 1
        total_clawed_back += cb

    return (applied_count, round(total_clawed_back, 2),
            skipped_not_commissioned, skipped_not_confirmed_paid, skipped_already_clawed,
            skipped_no_dropped_date)


def _list_cordoba_chargebacks(file, parsed):
    """Display-only, deliberately ungated: for every ID in the Chargebacks tab,
    look up the agent/dropped month from OUR OWN ClientRecord history and record
    a verbatim snapshot of the file row. Never touches gross/net commission."""
    chargebacks = parsed.get("chargebacks", [])
    incoming_ids = {row["crm_id"] for row in chargebacks if row["crm_id"]}
    if not incoming_ids:
        return 0, 0.0, []

    already_listed = {
        r[0] for r in db.session.query(CordobaChargebackEntry.crm_id)
        .filter(CordobaChargebackEntry.crm_id.in_(incoming_ids))
    }

    listed = 0
    total = 0.0
    skipped_no_match = []
    seen_this_file = set()
    for row in chargebacks:
        crm_id = row["crm_id"]
        if not crm_id or crm_id in seen_this_file:
            continue
        seen_this_file.add(crm_id)

        if crm_id in already_listed:
            continue

        candidates = ClientRecord.query.filter_by(crm_id=crm_id).order_by(ClientRecord.id.desc()).all()
        client_rec = next((c for c in candidates if c.dropped_date), None)
        dropped_period = _period_of(_parse_date(client_rec.dropped_date)) if client_rec else None
        if not dropped_period:
            skipped_no_match.append(_client_label(row.get("client_name"), crm_id))
            continue

        amount = row.get("marketing_payout_debt") or 0.0
        db.session.add(CordobaChargebackEntry(
            crm_id=crm_id,
            agent_name=client_rec.agent_name,
            period_label=dropped_period,
            assigned_company=row.get("assigned_company") or "",
            enrolled_date=row.get("enrolled_date"),
            client_name=row.get("client_name") or client_rec.client_name,
            status=row.get("status") or "",
            marketing_payout_debt=amount,
            first_payment_cleared_date=row.get("first_payment_cleared_date"),
            pay_freq=row.get("pay_freq") or "",
            payments_made=row.get("payments_made"),
            marketing_payment_cleared=row.get("marketing_payment_cleared"),
            marketing_payment_chargeback=row.get("marketing_payment_chargeback"),
            file_dropped_date=row.get("file_dropped_date"),
            uploaded_filename=file.filename,
        ))
        listed += 1
        total += amount

    return listed, round(total, 2), skipped_no_match


def process_cordoba_file(file):
    """Parse one Cordoba payout file and apply the paid-flag check, chargeback
    match-marking, chargeback clawback, and reconciliation listing. Commits."""
    file_bytes = file.read()
    parsed = parse_cordoba_payout(file_bytes)

    new_count, flipped = _apply_cordoba_paid_flags(file, parsed)
    matched_count, unmatched_chargeback_ids = _mark_cordoba_chargeback_matches(file, parsed)
    (clawback_count, clawback_total, skipped_not_commissioned,
     skipped_not_confirmed_paid, skipped_already_clawed,
     skipped_no_dropped_date) = _apply_cordoba_chargebacks(file, parsed)
    listed_count, listed_total, skipped_no_debt_match = _list_cordoba_chargebacks(file, parsed)

    db.session.commit()
    return {
        "errors": parsed["errors"],
        "new_paid_count": new_count, "flipped_count": flipped,
        "matched_count": matched_count, "unmatched_chargeback_ids": unmatched_chargeback_ids,
        "clawback_count": clawback_count, "clawback_total": clawback_total,
        "skipped_not_commissioned": skipped_not_commissioned,
        "skipped_not_confirmed_paid": skipped_not_confirmed_paid,
        "skipped_already_clawed": skipped_already_clawed,
        "skipped_no_dropped_date": skipped_no_dropped_date,
        "listed_count": listed_count, "listed_total": listed_total,
        "skipped_no_debt_match": skipped_no_debt_match,
    }
