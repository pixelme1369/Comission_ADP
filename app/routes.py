import csv
import io
from flask import Blueprint, render_template, request, redirect, url_for, flash, Response
from app import db
from app.models import CommissionPeriod, AgentCommission
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

    # Block duplicate period uploads
    existing = CommissionPeriod.query.filter_by(period_label=period_label).first()
    if existing:
        flash(
            f"Period {period_label} already exists (uploaded {existing.uploaded_at.strftime('%Y-%m-%d')}). "
            "Delete it first before re-uploading.",
            "error",
        )
        return redirect(url_for("main.index"))

    period = CommissionPeriod(
        period_label=period_label,
        filename=file.filename,
        total_agents=len(parsed["results"]),
    )
    db.session.add(period)
    db.session.flush()  # get period.id

    for r in parsed["results"]:
        agent = AgentCommission(period_id=period.id, **r)
        db.session.add(agent)

    db.session.commit()
    flash(f"Successfully processed {len(parsed['results'])} agents for period {period_label}.", "success")
    return redirect(url_for("main.period_detail", period_id=period.id))


@bp.route("/period/<int:period_id>")
def period_detail(period_id):
    period = CommissionPeriod.query.get_or_404(period_id)
    agents = AgentCommission.query.filter_by(period_id=period_id).order_by(AgentCommission.agent_name).all()

    total_payout = sum(a.payout for a in agents)
    total_commission = sum(a.gross_commission for a in agents)
    bonus_eligible = sum(1 for a in agents if a.quality_bonus_eligible)
    penalty_count = sum(1 for a in agents if a.cancellation_penalty_applied)
    nsf_count = sum(1 for a in agents if a.nsf_flagged)
    pending_count = sum(1 for a in agents if a.pending_units > 0)

    return render_template(
        "results.html",
        period=period,
        agents=agents,
        total_payout=total_payout,
        total_commission=total_commission,
        bonus_eligible=bonus_eligible,
        penalty_count=penalty_count,
        nsf_count=nsf_count,
        pending_count=pending_count,
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
        "Gross Commission", "Payout", "Payout Type",
        "Quality Bonus Eligible", "Cancel Penalty Applied",
        "NSF Flagged", "Pending Units", "Pending Debt", "Notes",
    ])
    for a in agents:
        writer.writerow([
            a.agent_name, a.units_cleared, f"{a.total_cleared_debt:.2f}",
            f"{a.cancellation_rate:.1f}",
            a.raw_tier, a.adjusted_tier, f"{a.tier_rate*100:.2f}",
            f"{a.gross_commission:.2f}", f"{a.payout:.2f}", a.payout_type,
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


@bp.route("/period/<int:period_id>/delete", methods=["POST"])
def delete_period(period_id):
    period = CommissionPeriod.query.get_or_404(period_id)
    db.session.delete(period)
    db.session.commit()
    flash(f"Period {period.period_label} deleted.", "success")
    return redirect(url_for("main.history"))


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
    all_errors = []

    for parsed in period_results:
        # Surface row-level warnings but don't block upload
        for err in parsed.get("errors", []):
            all_errors.append(err)

        if not parsed["results"] or not parsed["period_label"]:
            continue

        period_label = parsed["period_label"]
        existing = CommissionPeriod.query.filter_by(period_label=period_label).first()
        if existing:
            flash(
                f"Period {period_label} already exists (uploaded {existing.uploaded_at.strftime('%Y-%m-%d')}). "
                "Delete it first before re-uploading.",
                "error",
            )
            continue

        period = CommissionPeriod(
            period_label=period_label,
            filename=file.filename,
            total_agents=len(parsed["results"]),
        )
        db.session.add(period)
        db.session.flush()

        for r in parsed["results"]:
            agent = AgentCommission(period_id=period.id, **r)
            db.session.add(agent)

        db.session.commit()
        saved_period_ids.append((period.id, period_label, len(parsed["results"])))

    for err in all_errors:
        flash(err, "error")

    if not saved_period_ids:
        return redirect(url_for("main.index"))

    for pid, plabel, count in saved_period_ids:
        flash(f"CRM import: {count} agents processed for period {plabel}.", "success")

    if len(saved_period_ids) == 1:
        return redirect(url_for("main.period_detail", period_id=saved_period_ids[0][0]))

    return redirect(url_for("main.history"))


@bp.route("/history")
def history():
    periods = CommissionPeriod.query.order_by(CommissionPeriod.uploaded_at.desc()).all()
    return render_template("history.html", periods=periods)
