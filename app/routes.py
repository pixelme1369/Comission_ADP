import csv
import io
from flask import Blueprint, render_template, request, redirect, url_for, flash, Response
from app import db
from app.calculator import calculate_clawback_delta
from app.models import CommissionPeriod, AgentCommission, ClientRecord, CordobaPaidClient, CordobaChargeback
from app.csv_parser import parse_and_calculate
from app.crm_parser import parse_crm_and_calculate
from app.cordoba_parser import parse_cordoba_payout

bp = Blueprint("main", __name__)

ALLOWED_EXTENSIONS = {"csv"}
ALLOWED_XLSX_EXTENSIONS = {"xlsx"}


def _allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _allowed_xlsx_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_XLSX_EXTENSIONS


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
        r.crm_id for r in ClientRecord.query.filter_by(is_cleared=True).all() if r.crm_id
    }
    # Collect crm_ids Cordoba has already confirmed paying (from a prior weekly payout upload)
    already_cordoba_paid_ids = {p.crm_id for p in CordobaPaidClient.query.all()}
    period_results = parse_crm_and_calculate(file_bytes, file.filename, already_cleared_crm_ids)

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
                rec = ClientRecord(
                    period_id=period.id,
                    agent_commission_id=agent_obj.id,
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
                    is_late_activation=cr.get("is_late_activation", False),
                    original_cleared_period=cr.get("original_cleared_period"),
                    cordoba_paid=cr.get("crm_id") in already_cordoba_paid_ids,
                )
                db.session.add(rec)

            # Clawback clients — these cleared in a prior month, cancelled this month
            for cr in data["clawback_clients"]:
                rec = ClientRecord(
                    period_id=period.id,
                    agent_commission_id=agent_obj.id,
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
                    is_cleared=False,
                    is_pending=False,
                    is_cancelled=True,
                    commission_on_client=0.0,
                    clawback_applied=True,
                    clawback_period_id=period.id,
                    clawback_amount=cr.get("clawback_amount", 0.0),
                )
                db.session.add(rec)

        db.session.commit()
        saved_period_ids.append((period.id, period_label, len(parsed["results"])))

    if not saved_period_ids:
        return redirect(url_for("main.index"))

    for pid, plabel, count in saved_period_ids:
        flash(f"CRM import: {count} agents processed for period {plabel}.", "success")

    if len(saved_period_ids) == 1:
        return redirect(url_for("main.period_detail", period_id=saved_period_ids[0][0]))
    return redirect(url_for("main.history"))


def _get_or_create_holding_agent_commission(agent_name, period_label):
    """
    Find (or create) the AgentCommission row for an agent/period so a Cordoba-based
    clawback has somewhere to show up, even if the agent had zero cleared units that
    month according to CRM data. Mirrors the zero-unit holding entry the CRM parser
    itself creates for its own clawbacks.
    """
    period = CommissionPeriod.query.filter_by(period_label=period_label).first()
    if not period:
        period = CommissionPeriod(period_label=period_label, filename="(Cordoba payout only)", total_agents=0)
        db.session.add(period)
        db.session.flush()

    agent = AgentCommission.query.filter_by(period_id=period.id, agent_name=agent_name).first()
    if not agent:
        agent = AgentCommission(
            period_id=period.id, agent_name=agent_name,
            units_cleared=0, total_cleared_debt=0.0, cancellation_rate=0.0, hourly_draw=0.0,
            raw_tier=0, adjusted_tier=0, tier_rate=0.0, gross_commission=0.0,
            payout=0.0, payout_type="none", source="cordoba",
            notes="No cleared units this period from CRM data — this row only carries a "
                  "Cordoba-reported chargeback.",
        )
        db.session.add(agent)
        db.session.flush()
        period.total_agents = AgentCommission.query.filter_by(period_id=period.id).count()

    return agent


@bp.route("/upload-cordoba-payout", methods=["POST"])
def upload_cordoba_payout():
    """
    Upload Cordoba's weekly payout export (.xlsx: First Pays, EPF, Chargebacks tabs).

    Checks OUR existing commission data against Cordoba's data (not the reverse):
      - First Pays / EPF rows flip ClientRecord.cordoba_paid = True for any client we
        already have on file, and are remembered in CordobaPaidClient so future CRM
        uploads for the same client come in already marked paid.
      - Chargebacks rows are matched to our ClientRecord by crm_id to find which agent
        to attribute them to, then recorded as a separate CordobaChargeback amount
        (shown alongside, not merged into, the app's own predicted clawback).
    """
    file = request.files.get("cordoba_file")
    if not file or file.filename == "":
        flash("No file selected.", "error")
        return redirect(url_for("main.index"))
    if not _allowed_xlsx_file(file.filename):
        flash("Only .xlsx files are accepted for Cordoba payout uploads.", "error")
        return redirect(url_for("main.index"))

    file_bytes = file.read()
    parsed = parse_cordoba_payout(file_bytes)

    for err in parsed["errors"]:
        flash(err, "error")

    # 1. Remember every paid ID we haven't seen before (First Pays + EPF)
    new_paid_count = 0
    for row in parsed["paid_ids"]:
        crm_id = row["crm_id"]
        if not crm_id or CordobaPaidClient.query.filter_by(crm_id=crm_id).first():
            continue
        db.session.add(CordobaPaidClient(
            crm_id=crm_id, client_name=row.get("client_name"), source=row["source"],
            uploaded_filename=file.filename,
        ))
        new_paid_count += 1

    # 2. Flip cordoba_paid = True on every existing ClientRecord matching a paid ID
    all_paid_ids = {row["crm_id"] for row in parsed["paid_ids"] if row["crm_id"]}
    if all_paid_ids:
        ClientRecord.query.filter(
            ClientRecord.crm_id.in_(all_paid_ids), ClientRecord.cordoba_paid.is_(False)
        ).update({"cordoba_paid": True}, synchronize_session=False)

    # 3. Process chargebacks — match to our own ClientRecord to find the agent + original
    #    cleared-period commission, then compute the clawback via the same tier-delta rule
    #    used elsewhere in the app.
    matched_count = 0
    unmatched_count = 0
    for row in parsed["chargebacks"]:
        crm_id = row["crm_id"]
        if not crm_id or CordobaChargeback.query.filter_by(crm_id=crm_id).first():
            continue  # already recorded from a prior weekly upload

        client_record = ClientRecord.query.filter_by(crm_id=crm_id, is_cleared=True).first()

        cb_row = CordobaChargeback(
            crm_id=crm_id,
            client_name=row.get("client_name"),
            marketing_payout_debt=row.get("marketing_payout_debt", 0.0),
            orig_period=row.get("orig_period"),
            target_period=row.get("target_period"),
            chargeback_date=row.get("chargeback_date"),
            dropped_date=row.get("dropped_date"),
            uploaded_filename=file.filename,
        )

        if client_record and client_record.agent_commission:
            ac = client_record.agent_commission
            cb_row.agent_name = client_record.agent_name
            cb_row.matched = True
            cb_row.clawback_based_on_payout_amount = calculate_clawback_delta(
                orig_units=ac.units_cleared,
                orig_debt=ac.total_cleared_debt,
                orig_commission=ac.gross_commission,
                orig_cancellation_rate=ac.cancellation_rate,
                client_debt=row.get("marketing_payout_debt", 0.0),
            )
            matched_count += 1

            # Make sure the target period has somewhere to show this deduction, even if
            # this agent has zero cleared units that month from CRM data.
            if cb_row.target_period:
                _get_or_create_holding_agent_commission(client_record.agent_name, cb_row.target_period)
        else:
            unmatched_count += 1

        db.session.add(cb_row)

    db.session.commit()

    msg = f"Cordoba payout processed: {new_paid_count} newly confirmed paid file(s), " \
          f"{matched_count} chargeback(s) matched to an agent"
    if unmatched_count:
        msg += f", {unmatched_count} chargeback(s) had no matching client on file (check the ID)"
    flash(msg + ".", "success")
    return redirect(url_for("main.index"))


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

    # Cordoba payout reconciliation, per agent: how many cleared files are confirmed
    # paid by Cordoba, plus any chargebacks Cordoba sent us for this period.
    cordoba_stats = {}
    total_payout_clawback = 0.0
    for a in agents:
        cleared_clients = [c for c in a.clients if c.is_cleared]
        paid_count = sum(1 for c in cleared_clients if c.cordoba_paid)
        payout_clawback = db.session.query(db.func.sum(CordobaChargeback.clawback_based_on_payout_amount)).filter_by(
            target_period=period.period_label, agent_name=a.agent_name, matched=True
        ).scalar() or 0.0
        cordoba_stats[a.id] = {
            "paid": paid_count,
            "total": len(cleared_clients),
            "payout_clawback": payout_clawback,
        }
        total_payout_clawback += payout_clawback

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
        cordoba_stats=cordoba_stats,
        total_payout_clawback=total_payout_clawback,
    )


@bp.route("/period/<int:period_id>/agent/<int:agent_id>")
def agent_detail(period_id, agent_id):
    period = CommissionPeriod.query.get_or_404(period_id)
    agent = AgentCommission.query.get_or_404(agent_id)
    clients = ClientRecord.query.filter_by(agent_commission_id=agent_id).all()
    clawback_clients = [c for c in clients if c.clawback_applied]
    active_clients = [c for c in clients if not c.clawback_applied]

    payout_chargebacks = CordobaChargeback.query.filter_by(
        target_period=period.period_label, agent_name=agent.agent_name, matched=True
    ).all()

    return render_template(
        "agent_detail.html",
        period=period,
        agent=agent,
        clients=active_clients,
        clawback_clients=clawback_clients,
        payout_chargebacks=payout_chargebacks,
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
        "Cordoba Paid", "Clawback Based on Payout",
        "Quality Bonus Eligible", "Cancel Penalty Applied",
        "NSF Flagged", "Pending Units", "Pending Debt", "Notes",
    ])
    for a in agents:
        cleared_clients = [c for c in a.clients if c.is_cleared]
        paid_count = sum(1 for c in cleared_clients if c.cordoba_paid)
        payout_clawback = db.session.query(db.func.sum(CordobaChargeback.clawback_based_on_payout_amount)).filter_by(
            target_period=period.period_label, agent_name=a.agent_name, matched=True
        ).scalar() or 0.0
        writer.writerow([
            a.agent_name, a.units_cleared, f"{a.total_cleared_debt:.2f}",
            f"{a.cancellation_rate:.1f}",
            a.raw_tier, a.adjusted_tier, f"{a.tier_rate*100:.2f}",
            f"{a.gross_commission:.2f}", f"{a.clawback_amount:.2f}", f"{a.net_commission:.2f}",
            f"{paid_count}/{len(cleared_clients)}", f"{payout_clawback:.2f}",
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


@bp.route("/period/<int:period_id>/agent/<int:agent_id>/export")
def export_agent(period_id, agent_id):
    period = CommissionPeriod.query.get_or_404(period_id)
    agent = AgentCommission.query.get_or_404(agent_id)
    clients = ClientRecord.query.filter_by(agent_commission_id=agent_id).all()
    clawback_clients = [c for c in clients if c.clawback_applied]
    active_clients = [c for c in clients if not c.clawback_applied]

    payout_chargebacks = CordobaChargeback.query.filter_by(
        target_period=period.period_label, agent_name=agent.agent_name, matched=True
    ).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Type", "ID", "Client Name", "Enrolled Date", "Enrolled Debt", "Status",
        "1st Payment Cleared Date", "2nd Payment Cleared Date", "Dropped Date",
        "Payments Made", "# NSF",
        "Commission on Client", "Clawback Amount", "Cordoba Paid",
    ])
    for c in active_clients:
        t = "Cleared" if c.is_cleared else ("Pending" if c.is_pending else "Cancelled")
        writer.writerow([
            t, c.crm_id or "", c.client_name, c.enrolled_date or "",
            f"{c.enrolled_debt:.2f}", c.status,
            c.first_payment_cleared_date, c.second_payment_cleared_date or "",
            c.dropped_date or "",
            c.payments_made, c.nsf_count,
            f"{c.commission_on_client:.2f}", "",
            ("Yes" if c.cordoba_paid else "No") if c.is_cleared else "",
        ])
    for c in clawback_clients:
        writer.writerow([
            "Clawback", c.crm_id or "", c.client_name, c.enrolled_date or "",
            f"{c.enrolled_debt:.2f}", c.status,
            c.first_payment_cleared_date, c.second_payment_cleared_date or "",
            c.dropped_date or "",
            c.payments_made, c.nsf_count,
            "", f"-{c.clawback_amount:.2f}", "",
        ])
    for c in payout_chargebacks:
        writer.writerow([
            "Clawback Based on Payout", c.crm_id or "", c.client_name, "",
            f"{c.marketing_payout_debt:.2f}", "",
            "", "", c.dropped_date or "",
            "", "",
            "", f"-{c.clawback_based_on_payout_amount:.2f}", "",
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={agent.agent_name.replace(' ','_')}_{period.period_label}.csv"},
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
