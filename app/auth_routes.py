from datetime import datetime, timezone
from sqlalchemy import func
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from app import db
from app.models import AgentUser, AgentCommission, CommissionPeriod
from app.calculator import normalize_agent_name
from app.crm_parser import _payment_date_for_period
from app.auth import admin_required

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.index") if current_user.is_admin else url_for("auth.my_dashboard"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = AgentUser.query.filter_by(username=username).first()

        if not user or not check_password_hash(user.password_hash, password) or not user.active:
            flash("Invalid username or password.", "error")
            return redirect(url_for("auth.login"))

        login_user(user)
        user.last_login_at = datetime.now(timezone.utc)
        db.session.commit()

        next_url = request.args.get("next")
        if next_url:
            return redirect(next_url)
        return redirect(url_for("main.index") if user.is_admin else url_for("auth.my_dashboard"))

    return render_template("login.html")


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Logged out.", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/my-dashboard")
@login_required
def my_dashboard():
    if current_user.is_admin:
        return redirect(url_for("main.index"))

    target = normalize_agent_name(current_user.agent_name)
    agent_rows = (
        AgentCommission.query
        .join(CommissionPeriod, AgentCommission.period_id == CommissionPeriod.id)
        .filter(func.lower(func.trim(AgentCommission.agent_name)) == target)
        .order_by(CommissionPeriod.period_label.desc())
        .all()
    )

    next_commission = agent_rows[0] if agent_rows else None
    next_payout_date = (
        _payment_date_for_period(next_commission.period.period_label) if next_commission else None
    )

    return render_template(
        "my_dashboard.html",
        agent_rows=agent_rows,
        next_commission=next_commission,
        next_payout_date=next_payout_date,
    )


@auth_bp.route("/admin/agents", methods=["GET", "POST"])
@admin_required
def admin_agents():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        agent_name = (request.form.get("agent_name") or "").strip()

        if not username or not password or not agent_name:
            flash("Username, password, and agent name are all required.", "error")
            return redirect(url_for("auth.admin_agents"))

        if AgentUser.query.filter_by(username=username).first():
            flash(f"Username \"{username}\" is already taken.", "error")
            return redirect(url_for("auth.admin_agents"))

        db.session.add(AgentUser(
            username=username,
            password_hash=generate_password_hash(password),
            agent_name=agent_name,
            is_admin=False,
            active=True,
        ))
        db.session.commit()
        flash(f"Login created for {agent_name} (username: {username}).", "success")
        return redirect(url_for("auth.admin_agents"))

    known_agent_names = sorted({
        r[0] for r in db.session.query(AgentCommission.agent_name).distinct() if r[0]
    })
    agent_users = AgentUser.query.order_by(AgentUser.is_admin.desc(), AgentUser.username).all()

    return render_template(
        "admin_agents.html",
        agent_users=agent_users,
        known_agent_names=known_agent_names,
    )


@auth_bp.route("/admin/agents/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def reset_agent_password(user_id):
    user = AgentUser.query.get_or_404(user_id)
    new_password = request.form.get("password") or ""
    if not new_password:
        flash("A new password is required.", "error")
        return redirect(url_for("auth.admin_agents"))

    user.password_hash = generate_password_hash(new_password)
    db.session.commit()
    flash(f"Password reset for {user.username}.", "success")
    return redirect(url_for("auth.admin_agents"))


@auth_bp.route("/admin/agents/<int:user_id>/toggle-active", methods=["POST"])
@admin_required
def toggle_agent_active(user_id):
    user = AgentUser.query.get_or_404(user_id)
    if user.is_admin:
        flash("Cannot disable an admin account.", "error")
        return redirect(url_for("auth.admin_agents"))

    user.active = not user.active
    db.session.commit()
    flash(f"{user.username} is now {'active' if user.active else 'disabled'}.", "success")
    return redirect(url_for("auth.admin_agents"))
