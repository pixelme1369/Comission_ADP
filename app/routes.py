import csv
import io
from flask import Blueprint, render_template, request, redirect, url_for, flash, Response
from app import db
from app.models import (
    CommissionPeriod, AgentCommission, ClientRecord, CordobaPaidClient, CordobaChargedBackClient,
)
from app.csv_parser import parse_and_calculate
from app.crm_parser import parse_crm_and_calculate, _parse_date, _period_of
from app.cordoba_parser import parse_cordoba_payout
from app.commission_history_parser import parse_commission_history
from app.calculator import calculate_clawback_amount, get_fixed_rate

bp = Blueprint("main", __name__)

ALLOWED_EXTENSIONS = {"csv"}
ALLOWED_XLSX_EXTENSIONS = {"xlsx"}
ALLOWED_HISTORY_EXTENSIONS = {"xlsx", "csv"}


def _allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _allowed_xlsx_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_XLSX_EXTENSIONS


def _allowed_history_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_HISTORY_EXTENSIONS


def _new_client_record(period_id, agent_commission_id, cr, **overrides):
    """Build a ClientRecord from a parser client dict. Every upload flow saves clients
    through this one helper so the field mapping can never drift between flows;
    flow-specific values (clawback fields, cordoba_paid, ...) come in as overrides."""
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
        is_cleared=cr.get("is_cleared", False),
        is_pending=cr.get("is_pending", False),
        is_cancelled=cr.get("is_cancelled", False),
        commission_on_client=cr.get("commission_on_client", 0.0),
    )
    fields.update(overrides)
    return ClientRecord(**fields)


@bp.route("/")
def index():
    recent_periods = CommissionPeriod.query.order_by(CommissionPeriod.uploaded_at.desc()).limit(12).all()
    return render_template("index.html", periods=recent_periods)


@bp.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("csv_file")
    if not file or file.filename == "":
        flash("No file selected.", "error")
        return redirect(url_for("main.index"))
    if not _allowed_file(file.filename):
        flash("Only .csv files are accepted.", "error")
        return redirect(url_for("main.index"))

    file_bytes = file.read()
    parsed = parse_and_calculate(file_bytes, file.filename)

    if parsed["errors"]:
        for err in parsed["errors"]:
            flash(err, "error")
        return redirect(url_for("main.index"))

    period_label = parsed["period_label"]
    existing = CommissionPeriod.query.filter_by(period_label=period_label).first()
    if existing:
        flash(
            f"Period {period_label} already exists (uploaded {existing.uploaded_at.strftime('%Y-%m-%d')}). "
            "Delete it first before re-uploading.", "error",
        )
        return redirect(url_for("main.index"))

    period = CommissionPeriod(period_label=period_label, filename=file.filename,
                               total_agents=len(parsed["results"]))
    db.session.add(period)
    db.session.flush()

    for r in parsed["results"]:
        r.setdefault("clawback_amount", 0.0)
        r.setdefault("net_commission", r.get("gross_commission", 0.0))
        agent = AgentCommission(period_id=period.id, **r)
        db.session.add(agent)

    db.session.commit()
    flash(f"Successfully processed {len(parsed['results'])} agents for period {period_label}.", "success")
    return redirect(url_for("main.period_detail", period_id=period.id))


@bp.route("/upload-crm", methods=["POST"])
def upload_crm():
    file = request.files.get("csv_file")
    if not file or file.filename == "":
        flash("No file selected.", "error")
        return redirect(url_for("main.index"))
    if not _allowed_file(file.filename):
        flash("Only .csv files are accepted.", "error")
        return redirect(url_for("main.index"))

    file_bytes = file.read()

    # Collect crm_ids already saved as cleared so the parser can detect late activations
    already_cleared_crm_ids = {
        r[0] for r in db.session.query(ClientRecord.crm_id)
        .filter(ClientRecord.is_cleared.is_(True)) if r[0]
    }
    # Collect crm_ids Cordoba has already confirmed paying (from a prior payout upload)
    already_cordoba_paid_ids = {r[0] for r in db.session.query(CordobaPaidClient.crm_id)}
    # Collect crm_ids already clawed back via a Cordoba Chargebacks-tab upload, so this
    # CRM import doesn't claw the agent back a second time for the same client
    already_charged_back_crm_ids = {
        r[0] for r in db.session.query(CordobaChargedBackClient.crm_id) if r[0]
    }
    period_results = parse_crm_and_calculate(
        file_bytes, file.filename, already_cleared_crm_ids, already_charged_back_crm_ids
    )

    saved_period_ids = []
    shown_errors = set()

    for parsed in period_results:
        # Show row-level warnings once (they repeat across periods)
        for err in parsed.get("errors", []):
            if err not in shown_errors:
                flash(err, "error")
                shown_errors.add(err)

        if not parsed["results"] or not parsed["period_label"]:
            continue

        period_label = parsed["period_label"]
        existing = CommissionPeriod.query.filter_by(period_label=period_label).first()
        if existing:
            flash(
                f"Period {period_label} already exists (uploaded {existing.uploaded_at.strftime('%Y-%m-%d')}). "
                "Delete it first before re-uploading.", "error",
            )
            # A skipped period silently discards everything the parser computed for it —
            # including any NEW clawback (e.g. a Dropped Date backdated into an
            # already-uploaded month). Money must never disappear without a warning.
            # Clawbacks already recorded in the DB (normal monthly re-uploads recompute
            # them every time) are not warned about — only genuinely new ones.
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
                    detail = "; ".join(
                        f"{c['agent_name']} / {c.get('client_name') or c.get('crm_id') or 'unknown'}"
                        f" (${c.get('clawback_amount', 0.0):,.2f})"
                        for c in new_cb[:10]
                    )
                    more = f" and {len(new_cb) - 10} more" if len(new_cb) > 10 else ""
                    flash(
                        f"WARNING — {len(new_cb)} NEW clawback(s) fall in period {period_label} "
                        f"and were NOT applied: {detail}{more}. Delete period {period_label} and "
                        "re-upload this file to apply them.", "error",
                    )
            continue

        period = CommissionPeriod(
            period_label=period_label,
            filename=file.filename,
            total_agents=len(parsed["results"]),
        )
        db.session.add(period)
        db.session.flush()

        # Save agent commission records
        # Strip internal keys before saving to model
        agent_obj_map = {}  # agent_name → AgentCommission
        for r in parsed["results"]:
            cleared_clients = r.pop("_cleared_clients", [])
            all_period_clients = r.pop("_all_period_clients", [])
            clawback_clients = r.pop("_clawback_clients", [])
            r.pop("_period_label", None)

            agent_obj = AgentCommission(period_id=period.id, **r)
            db.session.add(agent_obj)
            db.session.flush()
            agent_obj_map[r["agent_name"]] = {
                "obj": agent_obj,
                "cleared_clients": cleared_clients,
                "all_period_clients": all_period_clients,
                "clawback_clients": clawback_clients,
            }

        # Save individual client records
        for agent_name, data in agent_obj_map.items():
            agent_obj = data["obj"]

            # Clients that belong to this period (cleared, pending, same-month cancel)
            for cr in data["all_period_clients"]:
                db.session.add(_new_client_record(
                    period.id, agent_obj.id, cr,
                    is_late_activation=cr.get("is_late_activation", False),
                    original_cleared_period=cr.get("original_cleared_period"),
                    cordoba_paid=cr.get("crm_id") in already_cordoba_paid_ids,
                ))

            # Clawback clients — these cleared in a prior month, cancelled this month
            for cr in data["clawback_clients"]:
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

        saved_period_ids.append((period.id, period_label, len(parsed["results"])))

    # One commit for the whole file: either every new period saves, or none do.
    # (Per-period commits could leave a half-imported file if a later period failed.)
    db.session.commit()

    if not saved_period_ids:
        return redirect(url_for("main.index"))

    for pid, plabel, count in saved_period_ids:
        flash(f"CRM import: {count} agents processed for period {plabel}.", "success")

    if len(saved_period_ids) == 1:
        return redirect(url_for("main.period_detail", period_id=saved_period_ids[0][0]))
    return redirect(url_for("main.history"))


def _apply_cordoba_paid_flags(file, parsed):
    """
    Check OUR existing ClientRecord IDs against Cordoba's First Pays / EPF tabs (not the
    reverse) — for every client we already have on file whose ID shows up in either tab,
    flip cordoba_paid = True.
    """
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

    # Deliberately not filtering on the current cordoba_paid value here (e.g. "IS False").
    # A row can end up with NULL instead of False if it was ever inserted while this
    # column didn't exist in the model (this happened once, see CLAUDE.md) — "IS False"
    # would silently skip NULL rows forever since NULL isn't equal to False in SQL.
    # Just unconditionally set every matching ID to True; re-setting an already-True row
    # is harmless.
    flipped = ClientRecord.query.filter(ClientRecord.crm_id.in_(incoming_ids)).update(
        {"cordoba_paid": True}, synchronize_session=False
    )

    return new_count, flipped


def _get_or_create_agent_period_row(period_label, agent_name, filename):
    """Find (or create a zero-unit) AgentCommission row to carry a clawback that has
    no cleared units of its own in this period — mirrors the CRM-clawback holding entry."""
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
            pending_units=0, pending_debt=0.0, source="crm", notes="",
        )
        db.session.add(agent_row)
        db.session.flush()
        period.total_agents = (period.total_agents or 0) + 1

    return period, agent_row


def _apply_cordoba_chargebacks(file, parsed):
    """
    Cross-reference Cordoba's Chargebacks tab against OUR OWN ClientRecord history — the
    tab has no agent/rep column, so "who gets charged back" comes from looking up each
    charged-back client ID in our records, not from the file. Two things must both be
    true before we claw anything back:
      1. We ever paid an agent commission on that client (ClientRecord.is_cleared was True)
      2. Cordoba has confirmed paying US on it at some point — the crm_id has appeared in
         a First Pays or EPF tab, ever (CordobaPaidClient ledger, not limited to this file)
    A chargeback logically can't exist without a prior payment, so #2 mainly catches data
    gaps (Cordoba's chargeback tab referencing an ID whose original payout confirmation we
    never uploaded) rather than filtering out real clawbacks.
    Claw back that agent's commission in the month the client dropped — unconditionally,
    regardless of the safe-payment-threshold that protects agents in the CRM-driven
    clawback flow, since Cordoba taking the marketing payout back from us is independent
    of that policy. Each crm_id is recorded in CordobaChargedBackClient forever so
    re-uploading this file, or a later CRM upload reflecting the same drop, never claws
    the agent back twice.
    """
    chargebacks = parsed.get("chargebacks", [])
    incoming_ids = {row["crm_id"] for row in chargebacks if row["crm_id"]}
    if not incoming_ids:
        return 0, 0.0, [], []

    already_charged_back = {
        c.crm_id for c in
        CordobaChargedBackClient.query.filter(CordobaChargedBackClient.crm_id.in_(incoming_ids)).all()
    }
    confirmed_paid_ids = {
        p.crm_id for p in
        CordobaPaidClient.query.filter(CordobaPaidClient.crm_id.in_(incoming_ids)).all()
    }
    # Third gate — never claw back a client who was ALREADY clawed back through any
    # other path: a CRM upload that reflected the drop (clawback_applied ClientRecord),
    # or a prior manager's "To subtract" row from a commission-history import (which
    # also creates a clawback_applied ClientRecord). Without this, the Cordoba
    # Chargebacks tab arriving AFTER the CRM export already clawed the agent back
    # would deduct the same client a second time — the CordobaChargedBackClient
    # ledger above only guards the Cordoba-first ordering.
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
    seen_this_file = set()

    for row in chargebacks:
        crm_id = row["crm_id"]
        if not crm_id or crm_id in already_charged_back or crm_id in seen_this_file:
            continue
        seen_this_file.add(crm_id)

        if crm_id in already_clawed_elsewhere:
            # Agent was already deducted for this client (CRM upload or history import).
            skipped_already_clawed.append(row.get("client_name") or crm_id)
            continue

        client_rec = (
            ClientRecord.query.filter_by(crm_id=crm_id, is_cleared=True)
            .order_by(ClientRecord.id.desc()).first()
        )
        if not client_rec:
            # We never recorded this client as cleared/commissioned — nothing to claw back.
            skipped_not_commissioned.append(row.get("client_name") or crm_id)
            continue

        if crm_id not in confirmed_paid_ids:
            # Cordoba's own First Pays/EPF tabs never confirmed paying us on this ID —
            # don't claw back an agent for a payout we can't verify we received.
            skipped_not_confirmed_paid.append(row.get("client_name") or crm_id)
            continue

        agent_name = client_rec.agent_name
        client_debt = client_rec.enrolled_debt or row.get("marketing_payout_debt", 0.0)
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

        dropped_date = row.get("dropped_date") or client_rec.dropped_date
        dropped_dt = _parse_date(dropped_date or "")
        orig_period_label = db.session.get(CommissionPeriod, client_rec.period_id).period_label
        dropped_period = _period_of(dropped_dt) or orig_period_label

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
            dropped_date=dropped_date,
            pay_freq=client_rec.pay_freq,
            payments_made=row.get("payments_made") or client_rec.payments_made,
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
            skipped_not_commissioned, skipped_not_confirmed_paid, skipped_already_clawed)


def _process_cordoba_file(file):
    """Parse one Cordoba payout file and apply both the paid-flag check (First Pays/EPF)
    and the chargeback-triggered agent clawback (Chargebacks tab)."""
    file_bytes = file.read()
    parsed = parse_cordoba_payout(file_bytes)

    for err in parsed["errors"]:
        flash(f"{file.filename}: {err}", "error")

    new_count, flipped = _apply_cordoba_paid_flags(file, parsed)
    (clawback_count, clawback_total, skipped_not_commissioned,
     skipped_not_confirmed_paid, skipped_already_clawed) = _apply_cordoba_chargebacks(file, parsed)

    db.session.commit()
    return (new_count, flipped, clawback_count, clawback_total,
            skipped_not_commissioned, skipped_not_confirmed_paid, skipped_already_clawed)


@bp.route("/upload-cordoba-payout", methods=["POST"])
def upload_cordoba_payout():
    """Upload one or more Cordoba payout files (.xlsx): First Pays/EPF flag confirmed
    payouts, Chargebacks tab triggers agent clawbacks for previously-paid clients."""
    files = [f for f in request.files.getlist("cordoba_file") if f and f.filename]
    if not files:
        flash("No file selected.", "error")
        return redirect(url_for("main.index"))

    bad_names = [f.filename for f in files if not _allowed_xlsx_file(f.filename)]
    if bad_names:
        flash(f"Only .xlsx files are accepted for Cordoba payout uploads: {', '.join(bad_names)}", "error")
        return redirect(url_for("main.index"))

    results = [_process_cordoba_file(file) for file in files]
    new_total = sum(r[0] for r in results)
    flipped_total = sum(r[1] for r in results)
    clawback_count_total = sum(r[2] for r in results)
    clawback_amount_total = sum(r[3] for r in results)
    skipped_not_commissioned = [name for r in results for name in r[4]]
    skipped_not_confirmed_paid = [name for r in results for name in r[5]]
    skipped_already_clawed = [name for r in results for name in r[6]]

    file_word = "file" if len(files) == 1 else f"{len(files)} files"
    flash(
        f"Cordoba payout processed ({file_word}): {new_total} newly recorded ID(s) in the ledger, "
        f"{flipped_total} client record(s) marked Cordoba Payout = Yes.",
        "success",
    )
    if clawback_count_total > 0:
        flash(
            f"Cordoba chargebacks: {clawback_count_total} client(s) charged back, "
            f"${clawback_amount_total:,.2f} clawed back from agent commissions.",
            "success",
        )

    def _flash_skipped(names, reason):
        if not names:
            return
        shown = ", ".join(names[:10])
        more = f" and {len(names) - 10} more" if len(names) > 10 else ""
        flash(f"{len(names)} charged-back client(s) {reason}: {shown}{more}.", "error")

    _flash_skipped(skipped_not_commissioned, "were never recorded as commissioned here — no clawback applied")
    _flash_skipped(skipped_not_confirmed_paid,
                   "were never confirmed paid via a First Pays/EPF upload — no clawback applied")
    _flash_skipped(skipped_already_clawed,
                   "were already clawed back via a CRM upload or history import — not deducted twice")
    return redirect(url_for("main.index"))


def _save_commission_history_period(period_label, results, filename, already_cordoba_paid_ids):
    """Save one month's worth of parsed historical-ledger results as a real
    CommissionPeriod + AgentCommission + ClientRecord rows, same shape the CRM flow
    produces, so this history is indistinguishable from a real upload for the purposes
    of Cordoba chargeback matching (_apply_cordoba_chargebacks looks up
    ClientRecord.is_cleared=True by crm_id, regardless of which upload flow created it)."""
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


@bp.route("/upload-commission-history", methods=["POST"])
def upload_commission_history():
    """Backfill past commission history from a prior account manager's ledger (.xlsx or
    .csv, NOT a CRM export — see commission_history_parser.py for the expected columns).
    Recreates real CommissionPeriod/AgentCommission/ClientRecord rows for those months
    so a later Cordoba Chargebacks-tab upload can find and claw back agents who were
    paid on a client before this app existed."""
    files = [f for f in request.files.getlist("history_file") if f and f.filename]
    if not files:
        flash("No file selected.", "error")
        return redirect(url_for("main.index"))

    bad_names = [f.filename for f in files if not _allowed_history_file(f.filename)]
    if bad_names:
        flash(f"Only .xlsx or .csv files are accepted for commission history uploads: {', '.join(bad_names)}", "error")
        return redirect(url_for("main.index"))

    year_raw = (request.form.get("history_year") or "").strip()
    if not year_raw.isdigit():
        flash("Please enter a valid year for the commission history file (the Month column has no year).", "error")
        return redirect(url_for("main.index"))
    year = int(year_raw)

    already_cordoba_paid_ids = {p.crm_id for p in CordobaPaidClient.query.all()}

    saved_period_ids = []
    total_periods_skipped = 0

    for file in files:
        file_bytes = file.read()
        parsed = parse_commission_history(file_bytes, file.filename, year)

        for err in parsed["errors"]:
            flash(f"{file.filename}: {err}", "error")

        for period_data in parsed["periods"]:
            period_label = period_data["period_label"]
            existing = CommissionPeriod.query.filter_by(period_label=period_label).first()
            if existing:
                flash(
                    f"Period {period_label} already exists (uploaded {existing.uploaded_at.strftime('%Y-%m-%d')}). "
                    "Delete it first before re-importing history for that month.", "error",
                )
                total_periods_skipped += 1
                continue

            period = _save_commission_history_period(
                period_label, period_data["results"], file.filename, already_cordoba_paid_ids
            )
            saved_period_ids.append(period.id)

        db.session.commit()

    if saved_period_ids:
        flash(f"Commission history import: {len(saved_period_ids)} month(s) backfilled.", "success")
    if total_periods_skipped:
        flash(f"{total_periods_skipped} month(s) skipped because a period already existed.", "error")

    if len(saved_period_ids) == 1:
        return redirect(url_for("main.period_detail", period_id=saved_period_ids[0]))
    return redirect(url_for("main.history"))


@bp.route("/period/<int:period_id>")
def period_detail(period_id):
    period = CommissionPeriod.query.get_or_404(period_id)
    agents = AgentCommission.query.filter_by(period_id=period_id).order_by(AgentCommission.agent_name).all()

    total_net = sum(a.net_commission for a in agents)
    total_gross = sum(a.gross_commission for a in agents)
    total_clawback = sum(a.clawback_amount for a in agents)
    bonus_eligible = sum(1 for a in agents if a.quality_bonus_eligible)
    penalty_count = sum(1 for a in agents if a.cancellation_penalty_applied)
    nsf_count = sum(1 for a in agents if a.nsf_flagged)
    pending_count = sum(1 for a in agents if a.pending_units > 0)

    return render_template(
        "results.html",
        period=period,
        agents=agents,
        total_net=total_net,
        total_gross=total_gross,
        total_clawback=total_clawback,
        bonus_eligible=bonus_eligible,
        penalty_count=penalty_count,
        nsf_count=nsf_count,
        pending_count=pending_count,
    )


@bp.route("/period/<int:period_id>/agent/<int:agent_id>")
def agent_detail(period_id, agent_id):
    period = CommissionPeriod.query.get_or_404(period_id)
    agent = AgentCommission.query.get_or_404(agent_id)
    clients = ClientRecord.query.filter_by(agent_commission_id=agent_id).all()
    clawback_clients = [c for c in clients if c.clawback_applied]
    active_clients = [c for c in clients if not c.clawback_applied]

    return render_template(
        "agent_detail.html",
        period=period,
        agent=agent,
        clients=active_clients,
        clawback_clients=clawback_clients,
    )


@bp.route("/period/<int:period_id>/export")
def export_period(period_id):
    period = CommissionPeriod.query.get_or_404(period_id)
    agents = AgentCommission.query.filter_by(period_id=period_id).order_by(AgentCommission.agent_name).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Agent Name", "Units Cleared", "Cleared Debt", "Cancel Rate %",
        "Raw Tier", "Adjusted Tier", "Rate %",
        "Gross Commission", "Clawback", "Net Commission",
        "Quality Bonus Eligible", "Cancel Penalty Applied",
        "NSF Flagged", "Pending Units", "Pending Debt", "Notes",
    ])
    for a in agents:
        writer.writerow([
            a.agent_name, a.units_cleared, f"{a.total_cleared_debt:.2f}",
            f"{a.cancellation_rate:.1f}",
            a.raw_tier, a.adjusted_tier, f"{a.tier_rate*100:.2f}",
            f"{a.gross_commission:.2f}", f"{a.clawback_amount:.2f}", f"{a.net_commission:.2f}",
            "Yes" if a.quality_bonus_eligible else "No",
            "Yes" if a.cancellation_penalty_applied else "No",
            "Yes" if a.nsf_flagged else "No",
            a.pending_units, f"{a.pending_debt:.2f}",
            a.notes,
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=commissions_{period.period_label}.csv"},
    )


CLIENT_EXPORT_COLUMNS = [
    "Type", "ID", "Client Name", "Enrolled Date", "Enrolled Debt", "Status",
    "1st Payment Cleared Date", "2nd Payment Cleared Date", "Dropped Date",
    "Payments Made", "Pay Freq.", "# NSF",
    "Commission on Client", "Clawback Amount", "Cordoba Payout",
]


def _client_export_rows(clients):
    clawback_clients = [c for c in clients if c.clawback_applied]
    active_clients = [c for c in clients if not c.clawback_applied]
    rows = []
    for c in active_clients:
        t = "Cleared" if c.is_cleared else ("Pending" if c.is_pending else "Cancelled")
        rows.append([
            t, c.crm_id or "", c.client_name, c.enrolled_date or "",
            f"{c.enrolled_debt:.2f}", c.status,
            c.first_payment_cleared_date, c.second_payment_cleared_date or "",
            c.dropped_date or "",
            c.payments_made, c.pay_freq or "", c.nsf_count,
            f"{c.commission_on_client:.2f}", "",
            ("Yes" if c.cordoba_paid else "No") if c.is_cleared else "",
        ])
    for c in clawback_clients:
        rows.append([
            "Clawback", c.crm_id or "", c.client_name, c.enrolled_date or "",
            f"{c.enrolled_debt:.2f}", c.status,
            c.first_payment_cleared_date, c.second_payment_cleared_date or "",
            c.dropped_date or "",
            c.payments_made, c.pay_freq or "", c.nsf_count,
            "", f"-{c.clawback_amount:.2f}", "",
        ])
    return rows


@bp.route("/period/<int:period_id>/agent/<int:agent_id>/export")
def export_agent(period_id, agent_id):
    period = CommissionPeriod.query.get_or_404(period_id)
    agent = AgentCommission.query.get_or_404(agent_id)
    clients = ClientRecord.query.filter_by(agent_commission_id=agent_id).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(CLIENT_EXPORT_COLUMNS)
    for row in _client_export_rows(clients):
        writer.writerow(row)

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={agent.agent_name.replace(' ','_')}_{period.period_label}.csv"},
    )


@bp.route("/period/<int:period_id>/export-all-agents")
def export_all_agents(period_id):
    period = CommissionPeriod.query.get_or_404(period_id)
    agents = AgentCommission.query.filter_by(period_id=period_id).order_by(AgentCommission.agent_name).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Agent Name"] + CLIENT_EXPORT_COLUMNS)
    for agent in agents:
        clients = ClientRecord.query.filter_by(agent_commission_id=agent.id).all()
        for row in _client_export_rows(clients):
            writer.writerow([agent.agent_name] + row)

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=all_agents_client_details_{period.period_label}.csv"},
    )


@bp.route("/period/<int:period_id>/delete", methods=["POST"])
def delete_period(period_id):
    period = CommissionPeriod.query.get_or_404(period_id)
    db.session.delete(period)
    db.session.commit()
    flash(f"Period {period.period_label} deleted.", "success")
    return redirect(url_for("main.history"))


@bp.route("/history")
def history():
    periods = CommissionPeriod.query.order_by(CommissionPeriod.uploaded_at.desc()).all()
    return render_template("history.html", periods=periods)
