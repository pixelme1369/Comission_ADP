import csv
import io
from flask import Blueprint, render_template, request, redirect, url_for, flash, Response
from app import db
from app.models import CommissionPeriod, AgentCommission, ClientRecord
from app.csv_parser import parse_and_calculate
from app.crm_parser import parse_crm_and_calculate

bp = Blueprint("main", __name__)

ALLOWED_EXTENSIONS = {"csv"}


def _allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


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


@bp.route("/period/<int:period_id>/agent/<int:agent_id>/export")
def export_agent(period_id, agent_id):
    period = CommissionPeriod.query.get_or_404(period_id)
    agent = AgentCommission.query.get_or_404(agent_id)
    clients = ClientRecord.query.filter_by(agent_commission_id=agent_id).all()
    clawback_clients = [c for c in clients if c.clawback_applied]
    active_clients = [c for c in clients if not c.clawback_applied]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Type", "ID", "Client Name", "Enrolled Date", "Enrolled Debt", "Status",
        "1st Payment Cleared Date", "2nd Payment Cleared Date", "Dropped Date",
        "Payments Made", "# NSF",
        "Commission on Client", "Clawback Amount",
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
        ])
    for c in clawback_clients:
        writer.writerow([
            "Clawback", c.crm_id or "", c.client_name, c.enrolled_date or "",
            f"{c.enrolled_debt:.2f}", c.status,
            c.first_payment_cleared_date, c.second_payment_cleared_date or "",
            c.dropped_date or "",
            c.payments_made, c.nsf_count,
            "", f"-{c.clawback_amount:.2f}",
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
