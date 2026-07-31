from functools import wraps

from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_user, logout_user, login_required, current_user

from agent_portal.models import Agent

bp = Blueprint("auth", __name__)


@bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("admin.dashboard" if current_user.is_admin else "agent.dashboard"))
    return redirect(url_for("auth.login"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("admin.dashboard" if current_user.is_admin else "agent.dashboard"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        agent = Agent.query.filter_by(email=email).first()
        if agent and agent.check_password(password):
            login_user(agent)
            next_url = request.args.get("next")
            if next_url:
                return redirect(next_url)
            return redirect(url_for("admin.dashboard" if agent.is_admin else "agent.dashboard"))
        flash("Invalid email or password.", "error")

    return render_template("login.html")


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


def admin_required(view_func):
    """Like @login_required, but also requires current_user.is_admin."""
    @wraps(view_func)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            flash("Admin access required.", "error")
            return redirect(url_for("agent.dashboard"))
        return view_func(*args, **kwargs)
    return wrapped


def agent_scope_names():
    """The set of exact `agent_name` strings (CRM "Sales Rep" spellings) the
    logged-in user is allowed to see. Used to filter every agent-facing query
    so one agent can never see another agent's commission data."""
    return current_user.alias_names()
