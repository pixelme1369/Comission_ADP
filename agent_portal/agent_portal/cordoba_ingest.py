"""Applies a parsed Cordoba payout file (see cordoba_parser.py) to the DB:
First Pays/EPF tabs flag ClientRecord.cordoba_paid = True (informational),
the Chargebacks tab triggers actual agent commission clawbacks. Ported from
app/routes.py's _apply_cordoba_paid_flags / _mark_cordoba_chargeback_matches /
_apply_cordoba_chargebacks / _list_cordoba_chargebacks / _process_cordoba_file
— same logic, same gates, adapted to this app's models/imports."""

from agent_portal import db
from commission_core.calculator import calculate_clawback_amount, get_fixed_rate
from commission_core.cordoba_parser import parse_cordoba_payout
from commission_core.crm_parser import _parse_date, _period_of
from agent_portal.models import (
    AgentCommission, ClientRecord, CommissionPeriod,
    CordobaChargebackEntry, CordobaChargebackMatchedClient,
    CordobaChargedBackClient, CordobaPaidClient,
)


def _client_label(client_name, crm_id):
    return f"{client_name} (ID {crm_id})" if client_name else crm_id


def cordoba_display_context(agent_row, clients):
    """Per-client "Cordoba Clawback" flag (matched, not necessarily deducted —
    see CordobaChargebackMatchedClient's docstring) plus the display-only
    "Cordoba Charge back" reconciliation rows for one agent's one period.
    Shared by both the agent-facing and admin-facing detail views."""
    crm_ids = {c.crm_id for c in clients if c.crm_id}
    cordoba_charged_back_ids = {
        cb.crm_id for cb in
        CordobaChargebackMatchedClient.query.filter(CordobaChargebackMatchedClient.crm_id.in_(crm_ids)).all()
    } if crm_ids else set()
    cordoba_chargeback_entries = CordobaChargebackEntry.query.filter_by(
        agent_name=agent_row.agent_name, period_label=agent_row.period.period_label,
    ).order_by(CordobaChargebackEntry.uploaded_at).all()
    return cordoba_charged_back_ids, cordoba_chargeback_entries


def merge_clawback_with_cordoba_entries(clawback_clients, cordoba_chargeback_entries):
    """Combines "Clawbacks Applied This Period" (real $ deductions) with
    "Cordoba Charge back" (a verbatim, looser-gated reconciliation listing —
    see CordobaChargebackEntry's own docstring) into ONE list for display,
    instead of two separate page sections (owner request, confirmed): every
    real clawback row gets a "Cordoba Charge back" Yes/No badge (Yes when a
    CordobaChargebackEntry also exists for that crm_id), and any crm_id
    that's ONLY in the Cordoba listing — Cordoba's file says charged back,
    but nothing has actually been deducted from the agent yet (most commonly:
    no Dropped Date on file for it yet) — is still shown, with $0.00 and Yes,
    rather than silently dropped from the page (owner confirmed: nothing
    should disappear, a $0.00 row just means "flagged, not deducted yet").

    Returns a list of plain dicts (not ClientRecord objects — the two
    sources don't share a model) with a uniform shape the template can
    render identically either way."""
    entry_by_crm_id = {e.crm_id: e for e in cordoba_chargeback_entries if e.crm_id}
    matched_crm_ids = set()
    merged = []

    for c in clawback_clients:
        merged.append({
            "crm_id": c.crm_id, "client_name": c.client_name, "enrolled_date": c.enrolled_date,
            "enrolled_debt": c.enrolled_debt, "credit_score": c.credit_score, "is_low_credit": c.is_low_credit,
            "first_payment_cleared_date": c.first_payment_cleared_date, "dropped_date": c.dropped_date,
            "payments_made": c.payments_made, "pay_freq": c.pay_freq, "clawback_amount": c.clawback_amount,
            "email": c.email, "phone": c.phone,
            "cordoba_charge_back": bool(c.crm_id) and c.crm_id in entry_by_crm_id,
        })
        if c.crm_id:
            matched_crm_ids.add(c.crm_id)

    for crm_id, e in entry_by_crm_id.items():
        if crm_id in matched_crm_ids:
            continue
        # Backfill from OUR OWN ClientRecord history when we have any row for
        # this crm_id, so this looks the same shape as a real deduction row
        # wherever possible — never from the Chargebacks file's own Marketing
        # Payout Debt column (see CLAUDE.md: that column is never used, even
        # as a fallback, for anything that touches an agent's dollars).
        own_record = ClientRecord.query.filter_by(crm_id=crm_id).order_by(ClientRecord.id.desc()).first()
        merged.append({
            "crm_id": crm_id,
            "client_name": (own_record.client_name if own_record else None) or e.client_name,
            "enrolled_date": own_record.enrolled_date if own_record else None,
            "enrolled_debt": own_record.enrolled_debt if own_record else 0.0,
            "credit_score": own_record.credit_score if own_record else None,
            "is_low_credit": own_record.is_low_credit if own_record else False,
            "first_payment_cleared_date": (
                own_record.first_payment_cleared_date if own_record else e.first_payment_cleared_date
            ),
            "dropped_date": own_record.dropped_date if own_record else None,
            "payments_made": own_record.payments_made if own_record else e.payments_made,
            "pay_freq": own_record.pay_freq if own_record else e.pay_freq,
            "clawback_amount": 0.0,
            "email": own_record.email if own_record else None,
            "phone": own_record.phone if own_record else None,
            "cordoba_charge_back": True,
        })

    return merged


def _get_or_create_agent_period_row(period_label, agent_name, filename):
    """Find (or create a zero-unit) AgentCommission row to carry a clawback that
    has no cleared units of its own in this period. A real, ongoing deduction
    against actual payout math — always the source="crm" period for this
    label (never the separate source="history_import" reference period, even
    if one happens to share the same period_label — see CommissionPeriod's
    docstring in models.py)."""
    period = CommissionPeriod.query.filter_by(period_label=period_label, source="crm").first()
    if not period:
        period = CommissionPeriod(period_label=period_label, filename=filename, total_agents=0, source="crm")
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
    """Display-only companion to _apply_cordoba_chargebacks: for every ID in the
    Chargebacks tab, look up the agent/dropped month from OUR OWN ClientRecord
    history and record a verbatim snapshot of the file row. Never touches
    gross/net commission.

    Gated on the client having actually been paid at some point (some
    ClientRecord row for this crm_id has is_cleared=True or
    clawback_applied=True) — a client who dropped before their commission was
    ever paid (same_month_cancel: cleared and dropped the same calendar
    month, shown under "Cancelled — Not Paid") has nothing to charge back, so
    listing them here would misrepresent a Cordoba chargeback as
    reconciliation-worthy when no money ever went out.

    UPSERT, not insert-once (owner request, confirmed): re-uploading a LATER
    Cordoba payout file with updated figures for a client already listed (more
    Payments Made, a corrected Marketing Payout Debt, etc.) used to be a silent
    no-op, leaving the reconciliation table stale forever. An existing
    CordobaChargebackEntry for a crm_id is now overwritten in place with this
    file's row, including uploaded_filename — so it always reflects whichever
    Cordoba file most recently reported that client, and deleting an OLDER
    upload no longer touches an entry a newer upload has since refreshed (see
    delete_cordoba_upload). Safe unconditionally: purely informational, unlike
    CordobaChargedBackClient's real-money ledger, whose never-claw-back-twice
    guard is untouched here."""
    chargebacks = parsed.get("chargebacks", [])
    incoming_ids = {row["crm_id"] for row in chargebacks if row["crm_id"]}
    if not incoming_ids:
        return 0, 0, 0.0, []

    existing_by_id = {
        e.crm_id: e for e in
        CordobaChargebackEntry.query.filter(CordobaChargebackEntry.crm_id.in_(incoming_ids))
    }

    listed = 0
    updated = 0
    total = 0.0
    skipped_no_match = []
    seen_this_file = set()
    for row in chargebacks:
        crm_id = row["crm_id"]
        if not crm_id or crm_id in seen_this_file:
            continue
        seen_this_file.add(crm_id)

        candidates = ClientRecord.query.filter_by(crm_id=crm_id).order_by(ClientRecord.id.desc()).all()
        was_paid = any(c.is_cleared or c.clawback_applied for c in candidates)
        client_rec = next((c for c in candidates if c.dropped_date), None)
        dropped_period = _period_of(_parse_date(client_rec.dropped_date)) if client_rec else None
        if not dropped_period or not was_paid:
            skipped_no_match.append(_client_label(row.get("client_name"), crm_id))
            continue

        amount = row.get("marketing_payout_debt") or 0.0
        fields = dict(
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
        )

        existing = existing_by_id.get(crm_id)
        if existing:
            for attr, value in fields.items():
                setattr(existing, attr, value)
            updated += 1
        else:
            db.session.add(CordobaChargebackEntry(crm_id=crm_id, **fields))
            listed += 1
        total += amount

    return listed, updated, round(total, 2), skipped_no_match


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
    listed_count, updated_count, listed_total, skipped_no_debt_match = _list_cordoba_chargebacks(file, parsed)

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
        "listed_count": listed_count, "updated_count": updated_count, "listed_total": listed_total,
        "skipped_no_debt_match": skipped_no_debt_match,
    }


def list_cordoba_uploads():
    """Groups every row across the four Cordoba ledger tables by
    uploaded_filename — the only "batch" identifier they share — into one
    summary per file, for the admin dashboard's "recent uploads" list.
    """
    filenames = set()
    for model in (CordobaPaidClient, CordobaChargedBackClient, CordobaChargebackMatchedClient, CordobaChargebackEntry):
        for row in db.session.query(model.uploaded_filename).distinct():
            if row[0]:
                filenames.add(row[0])

    summaries = []
    for filename in filenames:
        paid_rows = CordobaPaidClient.query.filter_by(uploaded_filename=filename).all()
        charged_back_rows = CordobaChargedBackClient.query.filter_by(uploaded_filename=filename).all()
        matched_rows = CordobaChargebackMatchedClient.query.filter_by(uploaded_filename=filename).all()
        entry_rows = CordobaChargebackEntry.query.filter_by(uploaded_filename=filename).all()
        timestamps = [r.uploaded_at for r in paid_rows + charged_back_rows + matched_rows + entry_rows if r.uploaded_at]
        summaries.append({
            "filename": filename,
            "uploaded_at": max(timestamps) if timestamps else None,
            "paid_count": len(paid_rows),
            "matched_count": len(matched_rows),
            "clawback_count": len(charged_back_rows),
            "clawback_total": round(sum(r.clawback_amount or 0.0 for r in charged_back_rows), 2),
            "listed_count": len(entry_rows),
        })

    summaries.sort(key=lambda s: s["uploaded_at"] or s["filename"], reverse=True)
    return summaries


def delete_cordoba_upload(filename):
    """Reverses everything a Cordoba payout upload did, keyed by filename (the
    ledger tables' only shared "batch" identifier). Used by the admin
    dashboard's delete/reset action so a wrong file can be fully undone:

    - Actual clawback deductions (CordobaChargedBackClient rows) are reversed:
      the money is added back to the agent's commission for that period, the
      holding ClientRecord row _apply_cordoba_chargebacks created is removed,
      and if that leaves the agent with no units/clawback/clients left in the
      period (i.e. it was a pure zero-unit holding row created just to carry
      this clawback), the AgentCommission row — and the period itself, if it
      was left with no agents — is removed too.
    - cordoba_paid is unflagged on every ClientRecord for a crm_id, but ONLY if
      no OTHER Cordoba upload also confirmed that same crm_id (checked after
      this file's CordobaPaidClient rows are removed).
    - The two display-only ledgers (CordobaChargebackMatchedClient's "Cordoba
      Clawback: Yes" badge, CordobaChargebackEntry's reconciliation listing)
      are simply cleared for this filename.

    Returns a summary dict of what was reversed, for a flash message.
    """
    charged_back_rows = CordobaChargedBackClient.query.filter_by(uploaded_filename=filename).all()
    reversed_amount = 0.0

    for row in charged_back_rows:
        # source="crm" — this reverses a deduction _get_or_create_agent_period_row
        # placed against the calculated period; see its docstring above.
        period = CommissionPeriod.query.filter_by(period_label=row.dropped_period, source="crm").first()
        agent_row = (
            AgentCommission.query.filter_by(period_id=period.id, agent_name=row.agent_name).first()
            if period else None
        )

        if agent_row:
            client_rec = ClientRecord.query.filter_by(
                agent_commission_id=agent_row.id, crm_id=row.crm_id, clawback_applied=True,
            ).first()
            if client_rec:
                db.session.delete(client_rec)

            agent_row.clawback_amount = max(
                0.0, round((agent_row.clawback_amount or 0.0) - (row.clawback_amount or 0.0), 2)
            )
            agent_row.net_commission = max(0.0, round(agent_row.gross_commission - agent_row.clawback_amount, 2))
            note = f"Cordoba chargeback: -${row.clawback_amount:,.2f} for {row.client_name or row.crm_id} (ID {row.crm_id})"
            if agent_row.notes:
                agent_row.notes = " | ".join(p for p in agent_row.notes.split(" | ") if p != note)
            db.session.flush()

            remaining_clients = ClientRecord.query.filter_by(agent_commission_id=agent_row.id).count()
            if agent_row.units_cleared == 0 and agent_row.clawback_amount == 0 and remaining_clients == 0:
                db.session.delete(agent_row)
                db.session.flush()
                if period and AgentCommission.query.filter_by(period_id=period.id).count() == 0:
                    db.session.delete(period)

        reversed_amount += row.clawback_amount or 0.0
        db.session.delete(row)

    matched_removed = CordobaChargebackMatchedClient.query.filter_by(uploaded_filename=filename).delete()
    entries_removed = CordobaChargebackEntry.query.filter_by(uploaded_filename=filename).delete()

    # Used to be one SELECT + one UPDATE per crm_id in this file (a First Pays/EPF
    # roster is easily thousands of rows — thousands of individual round trips was
    # the single slowest part of deleting a Cordoba upload). Same fix shape as
    # ingest.py's bulk_delete_period: batch the "is this crm_id still confirmed by
    # some OTHER upload" check into one query, then unflag the rest in one UPDATE.
    paid_crm_ids = {
        r[0] for r in db.session.query(CordobaPaidClient.crm_id)
        .filter_by(uploaded_filename=filename)
    }
    CordobaPaidClient.query.filter_by(uploaded_filename=filename).delete(synchronize_session=False)
    db.session.flush()

    still_confirmed = {
        r[0] for r in db.session.query(CordobaPaidClient.crm_id)
        .filter(CordobaPaidClient.crm_id.in_(paid_crm_ids))
    } if paid_crm_ids else set()
    to_unflag = paid_crm_ids - still_confirmed
    unflagged = ClientRecord.query.filter(ClientRecord.crm_id.in_(to_unflag)).update(
        {"cordoba_paid": False}, synchronize_session=False,
    ) if to_unflag else 0

    db.session.commit()
    return {
        "clawbacks_reversed": len(charged_back_rows),
        "amount_reversed": round(reversed_amount, 2),
        "matched_removed": matched_removed,
        "entries_removed": entries_removed,
        "paid_confirmations_removed": len(paid_crm_ids),
        "cordoba_paid_unflagged": unflagged,
    }
