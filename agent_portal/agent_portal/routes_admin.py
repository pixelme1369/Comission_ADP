from flask import Blueprint, current_app, jsonify, render_template, request, redirect, url_for, flash

from agent_portal import db
from agent_portal.auth import admin_required
from agent_portal.crm_parser import parse_crm_and_calculate
from agent_portal.drive_sync import sync_from_drive
from agent_portal.ingest import already_known_crm_id_sets, save_period_results
from agent_portal.models import Agent, AgentAlias, CommissionPeriod, SyncedFile

bp = Blueprint("admin", __name__, url_prefix="/admin")
cron_bp = Blueprint("cron", __name__, url_prefix="/cron")


@bp.route("/")
@admin_required
def dashboard():
    periods = CommissionPeriod.query.order_by(CommissionPeriod.period_label.desc()).all()
    agents = Agent.query.order_by(Agent.display_name).all()
    last_sync = SyncedFile.query.order_by(SyncedFile.synced_at.desc()).first()
    drive_configured = bool(current_app.config.get("GOOGLE_SERVICE_ACCOUNT_JSON"))
    return render_template(
        "admin_dashboard.html", periods=periods, agents=agents, last_sync=last_sync,
        drive_configured=drive_configured,
    )


@bp.route("/sync", methods=["POST"])
@admin_required
def sync_now():
    try:
        result = sync_from_drive()
    except Exception as exc:  # Drive/creds misconfiguration, network error, etc.
        flash(f"Drive sync failed: {exc}", "error")
        return redirect(url_for("admin.dashboard"))

    flash(result["message"], "success" if result["synced"] else "error")
    for w in result["warnings"]:
        flash(w, "error")
    return redirect(url_for("admin.dashboard"))


@bp.route("/upload-csv", methods=["POST"])
@admin_required
def upload_csv():
    """Manual CSV upload — same parser/persistence path as the Drive sync,
    kept as a convenience/debug fallback for testing without waiting on Drive."""
    file = request.files.get("csv_file")
    if not file or file.filename == "":
        flash("No file selected.", "error")
        return redirect(url_for("admin.dashboard"))
    if not file.filename.lower().endswith(".csv"):
        flash("Only .csv files are accepted.", "error")
        return redirect(url_for("admin.dashboard"))

    file_bytes = file.read()
    already_cleared, already_charged_back, already_low_credit = already_known_crm_id_sets()
    period_results = parse_crm_and_calculate(
        file_bytes, file.filename, already_cleared, already_charged_back, already_low_credit,
    )
    outcome = save_period_results(period_results, file.filename, source_label="manual")
    db.session.commit()

    if outcome["periods_created"]:
        flash(f"Imported {len(outcome['periods_created'])} period(s): {', '.join(outcome['periods_created'])}.", "success")
    else:
        flash("No new periods were created.", "error")
    for w in outcome["warnings"]:
        flash(w, "error")
    return redirect(url_for("admin.dashboard"))


@bp.route("/agents", methods=["GET", "POST"])
@admin_required
def manage_agents():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        display_name = (request.form.get("display_name") or "").strip()
        password = request.form.get("password") or ""
        is_admin = request.form.get("is_admin") == "on"
        alias_raw = request.form.get("agent_name") or ""

        if not email or not display_name or not password:
            flash("Email, display name, and password are all required.", "error")
        elif Agent.query.filter_by(email=email).first():
            flash(f"An account with email {email} already exists.", "error")
        else:
            agent = Agent(email=email, display_name=display_name, is_admin=is_admin)
            agent.set_password(password)
            db.session.add(agent)
            db.session.flush()
            if alias_raw.strip():
                db.session.add(AgentAlias(agent_id=agent.id, agent_name=alias_raw.strip()))
            db.session.commit()
            flash(f"Created account for {display_name} ({email}).", "success")
        return redirect(url_for("admin.manage_agents"))

    agents = Agent.query.order_by(Agent.display_name).all()
    return render_template("admin_agents.html", agents=agents)


@bp.route("/agents/<int:agent_id>/aliases", methods=["POST"])
@admin_required
def add_alias(agent_id):
    agent = Agent.query.get_or_404(agent_id)
    agent_name = (request.form.get("agent_name") or "").strip()
    if not agent_name:
        flash("Agent name (CRM Sales Rep spelling) is required.", "error")
    elif AgentAlias.query.filter_by(agent_name=agent_name).first():
        flash(f'"{agent_name}" is already mapped to another account.', "error")
    else:
        db.session.add(AgentAlias(agent_id=agent.id, agent_name=agent_name))
        db.session.commit()
        flash(f'Mapped "{agent_name}" to {agent.display_name}.', "success")
    return redirect(url_for("admin.manage_agents"))


@bp.route("/agents/aliases/<int:alias_id>/delete", methods=["POST"])
@admin_required
def delete_alias(alias_id):
    alias = AgentAlias.query.get_or_404(alias_id)
    db.session.delete(alias)
    db.session.commit()
    flash("Alias removed.", "success")
    return redirect(url_for("admin.manage_agents"))


@cron_bp.route("/sync", methods=["GET"])
def cron_sync():
    """Vercel Cron's daily hit (see vercel.json). No login session exists here,
    so CRON_SECRET is the only gate — configure it as an env var and Vercel
    sends it back as `Authorization: Bearer <CRON_SECRET>` automatically."""
    secret = current_app.config.get("CRON_SECRET")
    auth_header = request.headers.get("Authorization", "")
    if not secret or auth_header != f"Bearer {secret}":
        return jsonify({"error": "unauthorized"}), 401

    try:
        result = sync_from_drive()
    except Exception as exc:
        return jsonify({"synced": False, "error": str(exc)}), 500

    return jsonify(result)
