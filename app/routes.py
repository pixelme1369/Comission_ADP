import csv
import io
from flask import Blueprint, render_template, request, redirect, url_for, flash, Response
from app import db
from app.models import CommissionPeriod, AgentCommission, ClientRecord
from app.csv_parser import parse_and_calculate
from app.crm_parser import parse_crm_and_calculate
from app.clawback import calculate_clawbacks

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
    period_results = parse_crm_and_calculate(file_bytes, file.filename)

    saved_period_ids = []

    for parsed in period_results:
        for err in parsed.get("errors", []):
            flash(err, "error")

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

        period = CommissionPeriod(period_label=period_label, filename=file.filename,
                                   total_agents=len(parsed["results"]))
        db.session.add(period)
        db.session.flush()

        # Save agent commission records
        agent_map = {}  # agent_name → AgentCommission object
        for r in parsed["results"]:
            client_rows = r.pop("_client_rows", [])
            agent = AgentCommission(period_id=period.id, **r)
            db.session.add(agent)
            db.session.flush()
            agent_map[r["agent_name"]] = (agent, client_rows)

        # Save individual client records and link to agent commission
        client_record_map = {}  # (agent_name, crm_id) → ClientRecord
        for agent_name, (agent_obj, client_rows) in agent_map.items():
            for cr in client_rows:
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
                    payments_made=cr.get("payments_made", 0),
                    nsf_count=cr.get("nsf_count", 0),
                    enrolled_debt=cr.get("enrolled_debt", 0.0),
                    is_cleared=cr.get("is_cleared", False),
                    is_pending=cr.get("is_pending", False),
                    is_cancelled=cr.get("is_cancelled", False),
                    commission_on_client=cr.get("commission_on_client", 0.0),
                )
                # Store the cleared_period for clawback lookup later
                rec.cleared_period = cr.get("cleared_period")
                db.session.add(rec)
                client_record_map[(agent_name, cr.get("crm_id", ""))] = rec

        db.session.flush()

        # --- Clawback engine ---
        # Find clients in THIS file that cancelled in this period but cleared in a prior period
        clawback_candidates = []
        for agent_name, (agent_obj, client_rows) in agent_map.items():
            for cr in client_rows:
                if (cr.get("unit_status") == "clawback_candidate"
                        and cr.get("payments_made", 0) < 3
                        and cr.get("dropped_period") == period_label
                        and cr.get("cleared_period")
                        and cr["cleared_period"] != period_label):
                    # Find the stored ClientRecord for the original period
                    orig_client = ClientRecord.query.join(
                        CommissionPeriod, ClientRecord.period_id == CommissionPeriod.id
                    ).filter(
                        CommissionPeriod.period_label == cr["cleared_period"],
                        ClientRecord.agent_name == agent_name,
                        ClientRecord.crm_id == cr.get("crm_id"),
                    ).first()

                    if orig_client:
                        orig_client.cleared_period = cr["cleared_period"]
                        clawback_candidates.append((agent_name, agent_obj, orig_client))

        # Group clawback candidates by agent
        agent_clawback_clients = {}
        for agent_name, agent_obj, orig_client in clawback_candidates:
            agent_clawback_clients.setdefault((agent_name, agent_obj.id), (agent_obj, []))
            agent_clawback_clients[(agent_name, agent_obj.id)][1].append(orig_client)

        for (agent_name, _), (agent_obj, clients) in agent_clawback_clients.items():
            total_cb, per_client = calculate_clawbacks(agent_obj, clients, db.session)
            agent_obj.clawback_amount = total_cb
            agent_obj.net_commission = max(0.0, round(agent_obj.gross_commission - total_cb, 2))
            agent_obj.notes = (agent_obj.notes or "") + f" | Clawback ${total_cb:,.2f} deducted from {len(clients)} cancelled client(s)"

            for orig_client, cb_amt in per_client:
                orig_client.clawback_applied = True
                orig_client.clawback_period_id = period.id
                orig_client.clawback_amount = cb_amt

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

    # Also show clawback rows: clients from prior periods that were clawed back this period
    clawback_clients = ClientRecord.query.filter_by(clawback_period_id=period_id).filter(
        ClientRecord.agent_name == agent.agent_name
    ).all()

    return render_template(
        "agent_detail.html",
        period=period,
        agent=agent,
        clients=clients,
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
    clawback_clients = ClientRecord.query.filter_by(clawback_period_id=period_id).filter(
        ClientRecord.agent_name == agent.agent_name
    ).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Client Name", "Enrolled Debt", "Status", "1st Payment Cleared Date",
        "2nd Payment Cleared Date", "Payments Made", "# NSF",
        "Dropped Date", "Commission on Client", "Type", "Clawback Amount",
    ])
    for c in clients:
        writer.writerow([
            c.client_name, f"{c.enrolled_debt:.2f}", c.status,
            c.first_payment_cleared_date, c.second_payment_cleared_date,
            c.payments_made, c.nsf_count, c.dropped_date or "",
            f"{c.commission_on_client:.2f}",
            "Cleared" if c.is_cleared else ("Pending" if c.is_pending else "Cancelled"),
            "",
        ])
    for c in clawback_clients:
        writer.writerow([
            c.client_name, f"{c.enrolled_debt:.2f}", c.status,
            c.first_payment_cleared_date, c.second_payment_cleared_date,
            c.payments_made, c.nsf_count, c.dropped_date or "",
            "", "Clawback", f"{c.clawback_amount:.2f}",
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
