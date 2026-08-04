from functools import wraps

from flask import Blueprint, current_app, render_template, redirect, url_for, request, flash
from flask_login import login_user, logout_user, login_required, current_user
from google.auth.transport import requests as google_auth_requests
from google.oauth2 import id_token as google_id_token

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

    return render_template("login.html", google_client_id=current_app.config.get("GOOGLE_CLIENT_ID"))


@bp.route("/login/google", methods=["POST"])
def google_login():
    """Target of the Google Identity Services button's `data-login_uri` on the
    login page — Google POSTs a signed ID-token JWT here as `credential` after
    the user picks their Google account, no redirect round-trip or client
    secret involved. We verify the token came from Google and was issued for
    OUR client ID, then look the email up in our own Agent table — the admin
    adding an agent's email in Manage Agents IS the access grant. An
    unrecognized (but genuinely Google-verified) email is simply refused."""
    client_id = current_app.config.get("GOOGLE_CLIENT_ID")
    token = request.form.get("credential")
    if not client_id or not token:
        flash("Google sign-in is not configured.", "error")
        return redirect(url_for("auth.login"))

    try:
        claims = google_id_token.verify_oauth2_token(token, google_auth_requests.Request(), client_id)
    except ValueError:
        flash("Google sign-in failed — your session could not be verified. Please try again.", "error")
        return redirect(url_for("auth.login"))

    if not claims.get("email_verified", False):
        flash("Your Google account's email address is not verified.", "error")
        return redirect(url_for("auth.login"))

    email = (claims.get("email") or "").strip().lower()
    agent = Agent.query.filter_by(email=email).first()
    if not agent:
        flash(f"No portal account found for {email}. Ask your admin to add you in Manage Agents.", "error")
        return redirect(url_for("auth.login"))

    login_user(agent)
    return redirect(url_for("admin.dashboard" if agent.is_admin else "agent.dashboard"))


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
