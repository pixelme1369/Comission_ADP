from flask import Blueprint, current_app, jsonify, render_template, request, redirect, url_for, flash
from flask_login import current_user
from sqlalchemy import inspect as sa_inspect, text

from agent_portal import db
from agent_portal.auth import admin_required
from commission_core.calculator import units_to_next_tier, commission_gain_at_next_tier
from agent_portal.cordoba_ingest import (
    cordoba_display_context, delete_cordoba_upload, list_cordoba_uploads, process_cordoba_file,
)
from commission_core.crm_parser import parse_crm_and_calculate
from agent_portal.drive_sync import sync_from_drive
from agent_portal.history_ingest import allowed_history_file, import_commission_history_files
from agent_portal.ingest import (
    already_known_crm_id_sets, bulk_delete_period, delete_periods_by_filename,
    group_periods_by_filename, save_period_results,
)
from agent_portal.models import Agent, AgentAlias, AgentCommission, ClientRecord, CommissionPeriod, SyncedFile

CRM_SOURCES = ("drive", "manual")
HISTORY_SOURCES = ("history_import",)

bp = Blueprint("admin", __name__, url_prefix="/admin")
cron_bp = Blueprint("cron", __name__, url_prefix="/cron")


def _password_column_is_nullable():
    """Whether agent.password_hash currently allows NULL — False on a database
    created before "Sign in with Google" shipped (that column was originally
    NOT NULL). Used to surface a one-click fix on the dashboard instead of
    silently 500ing the moment someone leaves the password field blank; see
    run_nullable_password_migration below. Fails open (returns True, i.e. "no
    action needed") on any introspection error so a permissions hiccup here
    never blocks the rest of the dashboard from rendering."""
    try:
        for col in sa_inspect(db.engine).get_columns("agent"):
            if col["name"] == "password_hash":
                return bool(col["nullable"])
    except Exception:
        pass
    return True


# Must match migrate_widen_client_record_columns.py's COLUMNS list.
_WIDENED_CLIENT_RECORD_COLUMNS = {
    "crm_id": 100, "stage": 255, "status": 255, "submitted_date": 100,
    "enrolled_date": 100, "first_payment_date": 100, "first_payment_cleared_date": 100,
    "second_payment_cleared_date": 100, "dropped_date": 100, "pay_freq": 100,
}


def _client_record_columns_are_wide_enough():
    """Whether client_record's VARCHAR columns already match the widths in
    models.py — False on a database created before a real CRM export was
    found to overflow one of them (originally sized off a single sample
    row). See run_widen_client_record_columns_migration below. Fails open
    (True, "no action needed") on any introspection error, same reasoning
    as _password_column_is_nullable above."""
    try:
        cols = {c["name"]: c for c in sa_inspect(db.engine).get_columns("client_record")}
        for name, wanted_len in _WIDENED_CLIENT_RECORD_COLUMNS.items():
            col = cols.get(name)
            actual_len = getattr(col["type"], "length", None) if col else None
            if actual_len is not None and actual_len < wanted_len:
                return False
    except Exception:
        pass
    return True


@bp.route("/")
@admin_required
def dashboard():
    periods = CommissionPeriod.query.order_by(CommissionPeriod.period_label.desc()).all()
    # CommissionPeriod.source is the definitive split (see its docstring in
    # models.py) — calculated ("crm") and historical ("history_import") data
    # for the SAME period_label can legitimately coexist as two separate rows
    # here, e.g. two "2026-05" entries, one in each list below.
    crm_periods = [p for p in periods if p.source == "crm"]
    history_periods = [p for p in periods if p.source == "history_import"]
    agents = Agent.query.order_by(Agent.display_name).all()
    last_sync = SyncedFile.query.order_by(SyncedFile.synced_at.desc()).first()
    drive_configured = bool(current_app.config.get("GOOGLE_SERVICE_ACCOUNT_JSON"))
    return render_template(
        "admin_dashboard.html", agents=agents, last_sync=last_sync,
        drive_configured=drive_configured,
        password_migration_needed=not _password_column_is_nullable(),
        column_widen_migration_needed=not _client_record_columns_are_wide_enough(),
        crm_uploads=group_periods_by_filename(crm_periods),
        history_uploads=group_periods_by_filename(history_periods),
        cordoba_uploads=list_cordoba_uploads(),
    )


@bp.route("/migrate/nullable-password", methods=["POST"])
@admin_required
def run_nullable_password_migration():
    """One-click fix for databases created before "Sign in with Google"
    shipped: agent.password_hash was originally NOT NULL, so creating a
    Google-sign-in-only agent (blank password) 500s until this runs. Same SQL
    as migrate_nullable_password.py, just triggerable from the admin UI
    instead of needing local Python + DATABASE_URL access. Safe to click more
    than once — dropping a constraint that's already gone is a no-op on
    Postgres, not an error."""
    try:
        db.session.execute(text("ALTER TABLE agent ALTER COLUMN password_hash DROP NOT NULL"))
        db.session.commit()
        flash("Migration applied: you can now create agent accounts with no password (Google sign-in only).", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Migration failed: {exc}", "error")
    return redirect(url_for("admin.dashboard"))


@bp.route("/migrate/widen-client-record-columns", methods=["POST"])
@admin_required
def run_widen_client_record_columns_migration():
    """One-click fix for databases created before client_record's VARCHAR
    columns were widened: a real CRM export with a longer value than the
    original (guessed) limit in one of them — an ID, a verbose Status/Stage
    string, etc. — used to crash the whole upload with a raw "value too
    long for type character varying(N)" Postgres error and no exception
    handling around it, surfacing as a bare Internal Server Error. Same SQL
    as migrate_widen_client_record_columns.py. Safe to click more than
    once — widening an already-wide-enough column is a no-op on Postgres."""
    try:
        for column, new_length in _WIDENED_CLIENT_RECORD_COLUMNS.items():
            db.session.execute(text(
                f"ALTER TABLE client_record ALTER COLUMN {column} TYPE VARCHAR({new_length})"
            ))
        db.session.commit()
        flash("Migration applied: client_record columns widened — longer CRM values will no longer fail to import.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Migration failed: {exc}", "error")
    return redirect(url_for("admin.dashboard"))


@bp.route("/period/<int:period_id>")
@admin_required
def period_detail(period_id):
    """Admin view of one commission period — every agent who has a row in it,
    unlike the agent-facing dashboard which is scoped to one agent and only
    the current period."""
    period = CommissionPeriod.query.get_or_404(period_id)
    agents = AgentCommission.query.filter_by(period_id=period_id).order_by(AgentCommission.agent_name).all()
    units_to_next_tier_map = {a.id: units_to_next_tier(a.units_cleared, a.agent_name) for a in agents}
    # "Clawback Files" — every client dropped without hitting the safe payment
    # threshold, deducted from THIS period's payout, across every agent at
    # once. clawback_period_id/period_id both point at the period the
    # deduction actually landed in (crm_parser.py's "latest period in file"
    # rule, NOT necessarily the client's own dropped month) — see CLAUDE.md's
    # Clawback Rules. Previously this was only visible one agent at a time on
    # their own detail page; this is the same underlying ClientRecord rows,
    # just queried across the whole period for one audit view.
    clawback_files = (
        ClientRecord.query.filter_by(period_id=period_id, clawback_applied=True)
        .order_by(ClientRecord.agent_name, ClientRecord.dropped_date).all()
    )
    clawback_files_total = round(sum(c.clawback_amount or 0.0 for c in clawback_files), 2)
    return render_template(
        "admin_period_detail.html", period=period, agents=agents,
        units_to_next_tier_map=units_to_next_tier_map,
        clawback_files=clawback_files, clawback_files_total=clawback_files_total,
    )


@bp.route("/period/<int:period_id>/delete", methods=["POST"])
@admin_required
def delete_period(period_id):
    """Deletes a period so it can be re-imported — e.g. after a parser fix
    that only takes effect on rows processed after the fix shipped. Uses
    bulk_delete_period (see ingest.py) instead of the ORM cascade, which
    measurably turns into hundreds-to-thousands of individual DELETE
    round-trips for a period with real data."""
    period = CommissionPeriod.query.get_or_404(period_id)
    period_label = period.period_label
    bulk_delete_period(period)
    db.session.commit()
    flash(f"Period {period_label} deleted. Re-upload its CRM export to re-import it.", "success")
    return redirect(url_for("admin.dashboard"))


@bp.route("/uploads/crm/delete", methods=["POST"])
@admin_required
def delete_crm_upload():
    """Deletes every period a single CRM-export upload created (it can span
    several months in one file) in one action, instead of one period at a
    time from the period detail page."""
    filename = request.form.get("filename") or ""
    deleted = delete_periods_by_filename(filename, CRM_SOURCES)
    if deleted:
        flash(f"Deleted {len(deleted)} period(s) from \"{filename}\": {', '.join(deleted)}. "
              "Re-upload the CRM export to re-import them.", "success")
    else:
        flash(f"No CRM-export periods found for \"{filename}\".", "error")
    return redirect(url_for("admin.dashboard"))


@bp.route("/uploads/history/delete", methods=["POST"])
@admin_required
def delete_history_upload():
    """Deletes every period a single commission-history-backfill upload
    created, in one action."""
    filename = request.form.get("filename") or ""
    deleted = delete_periods_by_filename(filename, HISTORY_SOURCES)
    if deleted:
        flash(f"Deleted {len(deleted)} backfilled period(s) from \"{filename}\": {', '.join(deleted)}. "
              "Re-upload the history file to re-import them.", "success")
    else:
        flash(f"No commission-history periods found for \"{filename}\".", "error")
    return redirect(url_for("admin.dashboard"))


@bp.route("/uploads/cordoba/delete", methods=["POST"])
@admin_required
def delete_cordoba_upload_route():
    """Fully reverses one Cordoba payout upload: reinstates any clawed-back
    commission, un-flags Cordoba Payout confirmations no other file also
    confirmed, and clears the two display-only ledgers. See
    cordoba_ingest.delete_cordoba_upload for exactly what this undoes."""
    filename = request.form.get("filename") or ""
    result = delete_cordoba_upload(filename)
    if result["clawbacks_reversed"]:
        flash(
            f"Reversed {result['clawbacks_reversed']} clawback(s) from \"{filename}\" "
            f"(${result['amount_reversed']:,.2f} restored to agent commissions).", "success",
        )
    flash(
        f"\"{filename}\" reset: {result['paid_confirmations_removed']} paid confirmation(s) removed "
        f"({result['cordoba_paid_unflagged']} client record(s) un-flagged), "
        f"{result['matched_removed']} chargeback match(es) and {result['entries_removed']} "
        "reconciliation entry(ies) cleared.", "success",
    )
    return redirect(url_for("admin.dashboard"))


@bp.route("/period/<int:period_id>/agent/<int:agent_commission_id>")
@admin_required
def agent_detail(period_id, agent_commission_id):
    """Admin view of one agent's full client breakdown in one period — any
    period, any agent, no current-period-only or own-name-only restriction
    (those only apply to the agent-facing portal)."""
    agent_row = AgentCommission.query.filter_by(id=agent_commission_id, period_id=period_id).first_or_404()
    clients = ClientRecord.query.filter_by(agent_commission_id=agent_row.id).all()
    clawback_clients = [c for c in clients if c.clawback_applied]
    active_clients = [c for c in clients if not c.clawback_applied]
    cordoba_charged_back_ids, cordoba_chargeback_entries = cordoba_display_context(agent_row, active_clients)
    return render_template(
        "admin_agent_detail.html", agent=agent_row, period=agent_row.period,
        clients=active_clients, clawback_clients=clawback_clients,
        cordoba_charged_back_ids=cordoba_charged_back_ids,
        cordoba_chargeback_entries=cordoba_chargeback_entries,
        units_to_next_tier=units_to_next_tier(agent_row.units_cleared, agent_row.agent_name),
        commission_gain_at_next_tier=commission_gain_at_next_tier(
            agent_row.adjusted_tier, agent_row.total_cleared_debt, agent_row.gross_commission,
            agent_row.agent_name,
        ),
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
    already_cleared, already_charged_back, already_low_credit, already_history_paid = already_known_crm_id_sets()
    try:
        period_results = parse_crm_and_calculate(
            file_bytes, file.filename, already_cleared, already_charged_back, already_low_credit,
            already_history_paid,
            # agent_portal-specific policy flags — see commission_core/crm_parser.py's
            # module docstring for the owner-confirmed reasoning behind all three.
            persist_same_month_cancel=True,
            require_prior_payment_evidence=False,
        )
        outcome = save_period_results(period_results, file.filename, source_label="manual")
        db.session.commit()
    except Exception as exc:
        # Anything unexpected (a value too long for a column, a malformed
        # row the parser didn't anticipate, etc.) used to bubble all the way
        # up as a raw 500 "Internal Server Error" with no indication of what
        # went wrong or that nothing was saved. Roll back so a half-applied
        # import can never linger, and tell the admin what actually broke.
        db.session.rollback()
        flash(f"Import failed — nothing was saved. Error: {exc}", "error")
        return redirect(url_for("admin.dashboard"))

    if outcome["periods_created"]:
        flash(f"Imported {len(outcome['periods_created'])} period(s): {', '.join(outcome['periods_created'])}.", "success")
    else:
        flash("No new periods were created.", "error")
    for w in outcome["warnings"]:
        flash(w, "error")
    return redirect(url_for("admin.dashboard"))


@bp.route("/upload-cordoba-payout", methods=["POST"])
@admin_required
def upload_cordoba_payout():
    """Upload one or more Cordoba payout files (.xlsx): First Pays/EPF flag
    confirmed payouts, Chargebacks tab triggers agent clawbacks for
    previously-paid clients. Same logic as the internal app's flow."""
    files = [f for f in request.files.getlist("cordoba_file") if f and f.filename]
    if not files:
        flash("No file selected.", "error")
        return redirect(url_for("admin.dashboard"))

    bad_names = [f.filename for f in files if not f.filename.lower().endswith(".xlsx")]
    if bad_names:
        flash(f"Only .xlsx files are accepted for Cordoba payout uploads: {', '.join(bad_names)}", "error")
        return redirect(url_for("admin.dashboard"))

    try:
        results = [process_cordoba_file(file) for file in files]
    except Exception as exc:
        db.session.rollback()
        flash(f"Cordoba payout processing failed — nothing was saved. Error: {exc}", "error")
        return redirect(url_for("admin.dashboard"))

    for r in results:
        for err in r["errors"]:
            flash(err, "error")

    new_total = sum(r["new_paid_count"] for r in results)
    flipped_total = sum(r["flipped_count"] for r in results)
    matched_total = sum(r["matched_count"] for r in results)
    clawback_count_total = sum(r["clawback_count"] for r in results)
    clawback_amount_total = sum(r["clawback_total"] for r in results)
    listed_total = sum(r["listed_count"] for r in results)
    listed_amount_total = sum(r["listed_total"] for r in results)

    file_word = "file" if len(files) == 1 else f"{len(files)} files"
    flash(
        f"Cordoba payout processed ({file_word}): {new_total} newly recorded ID(s) in the ledger, "
        f"{flipped_total} client record(s) marked Cordoba Payout = Yes.",
        "success",
    )
    if matched_total > 0:
        flash(f"Cordoba chargebacks: {matched_total} client(s) matched and marked \"Cordoba Clawback: Yes\".", "success")
    if clawback_count_total > 0:
        flash(
            f"Cordoba chargebacks: {clawback_count_total} client(s) charged back, "
            f"${clawback_amount_total:,.2f} clawed back from agent commissions.",
            "success",
        )
    if listed_total > 0:
        flash(
            f"Cordoba Charge back: {listed_total} client(s) (${listed_amount_total:,.2f} total "
            "Marketing Payout Debt) listed for reference — informational only, not deducted.",
            "success",
        )

    def _flash_skipped(names, reason):
        if not names:
            return
        shown = ", ".join(names[:10])
        more = f" and {len(names) - 10} more" if len(names) > 10 else ""
        flash(f"{len(names)} charged-back client(s) {reason}: {shown}{more}.", "error")

    for r in results:
        _flash_skipped(r["skipped_not_commissioned"], "were never recorded as commissioned here — no clawback applied")
        _flash_skipped(r["skipped_not_confirmed_paid"],
                       "were never confirmed paid via a First Pays/EPF upload — no clawback applied")
        _flash_skipped(r["skipped_already_clawed"], "were already clawed back elsewhere — not deducted twice")
        _flash_skipped(r["skipped_no_dropped_date"],
                       "have no Dropped Date recorded in our own CRM data yet — import a CRM export "
                       "reflecting the drop, then re-upload this Chargebacks file")
        _flash_skipped(r["unmatched_chargeback_ids"], "were not found in any of our commission reports — no match")
        _flash_skipped(r["skipped_no_debt_match"],
                       "were not found in our commission reports, have no Dropped Date on file yet, or were "
                       "never actually paid commission (dropped before payout) — not listed under "
                       "\"Cordoba Charge back\"")


@bp.route("/upload-commission-history", methods=["POST"])
@admin_required
def upload_commission_history():
    """Backfill past commission history from a prior account manager's ledger
    (.xlsx or .csv, NOT a CRM export). Recreates real periods so a later
    Cordoba Chargebacks upload can find and claw back agents paid on a
    client before this portal existed."""
    files = [f for f in request.files.getlist("history_file") if f and f.filename]
    if not files:
        flash("No file selected.", "error")
        return redirect(url_for("admin.dashboard"))

    bad_names = [f.filename for f in files if not allowed_history_file(f.filename)]
    if bad_names:
        flash(f"Only .xlsx or .csv files are accepted for commission history uploads: {', '.join(bad_names)}", "error")
        return redirect(url_for("admin.dashboard"))

    year_raw = (request.form.get("history_year") or "").strip()
    if not year_raw.isdigit():
        flash("Please enter a valid year for the commission history file (the Month column has no year).", "error")
        return redirect(url_for("admin.dashboard"))
    year = int(year_raw)

    try:
        outcome = import_commission_history_files(files, year)
    except Exception as exc:
        db.session.rollback()
        flash(f"Commission history import failed — nothing was saved. Error: {exc}", "error")
        return redirect(url_for("admin.dashboard"))

    if outcome["saved_period_ids"]:
        flash(f"Commission history import: {len(outcome['saved_period_ids'])} month(s) backfilled.", "success")
    if outcome["periods_skipped"]:
        flash(
            f"{outcome['periods_skipped']} month(s) skipped — that month's commission history was "
            "already imported (an existing calculated period for the same month does NOT skip it).",
            "error",
        )
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

        # Password is optional — an account created without one can only sign in
        # with "Sign in with Google" (google_login in auth.py matches on email;
        # adding the email here IS the access grant). Set one later any time via
        # "Reset Password" below to also allow email/password login.
        if not email or not display_name:
            flash("Email and display name are required.", "error")
        elif Agent.query.filter_by(email=email).first():
            flash(f"An account with email {email} already exists.", "error")
        elif password and len(password) < 6:
            flash("Password must be at least 6 characters — or leave it blank for Google sign-in only.", "error")
        else:
            agent = Agent(email=email, display_name=display_name, is_admin=is_admin)
            if password:
                agent.set_password(password)
            db.session.add(agent)
            try:
                db.session.flush()
            except Exception:
                # Most likely cause: a database created before "Sign in with Google"
                # shipped still has agent.password_hash as NOT NULL, and this account
                # was left password-less. Point at the one-click fix on the dashboard
                # instead of surfacing a raw 500.
                db.session.rollback()
                flash(
                    "Couldn't create that account. If you left the password blank, this "
                    "database needs a one-time migration first — go to Admin and click "
                    "\"Fix Now\" under Google Sign-In Setup, then try again.",
                    "error",
                )
                return redirect(url_for("admin.manage_agents"))
            if alias_raw.strip():
                db.session.add(AgentAlias(agent_id=agent.id, agent_name=alias_raw.strip()))
            db.session.commit()
            mode = "password + Google sign-in" if password else "Google sign-in only"
            flash(f"Created account for {display_name} ({email}) — {mode}.", "success")
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


@bp.route("/agents/<int:agent_id>/email", methods=["POST"])
@admin_required
def update_email(agent_id):
    """Lets the admin correct/change an agent's login email in place — the
    same address is both the email/password login and the Google
    sign-in match key (see auth.py::google_login), so a typo here otherwise
    has no fix short of deleting and recreating the account."""
    agent = Agent.query.get_or_404(agent_id)
    new_email = (request.form.get("email") or "").strip().lower()
    if not new_email:
        flash("Email is required.", "error")
    elif Agent.query.filter(Agent.email == new_email, Agent.id != agent.id).first():
        flash(f"Another account already uses {new_email}.", "error")
    elif new_email == agent.email:
        flash("That's already this agent's email.", "error")
    else:
        old_email = agent.email
        agent.email = new_email
        db.session.commit()
        flash(f"Updated email for {agent.display_name}: {old_email} → {new_email}.", "success")
    return redirect(url_for("admin.manage_agents"))


@bp.route("/agents/aliases/<int:alias_id>/delete", methods=["POST"])
@admin_required
def delete_alias(alias_id):
    alias = AgentAlias.query.get_or_404(alias_id)
    db.session.delete(alias)
    db.session.commit()
    flash("Alias removed.", "success")
    return redirect(url_for("admin.manage_agents"))


@bp.route("/agents/<int:agent_id>/password", methods=["POST"])
@admin_required
def reset_password(agent_id):
    agent = Agent.query.get_or_404(agent_id)
    new_password = request.form.get("password") or ""
    if len(new_password) < 6:
        flash("Password must be at least 6 characters.", "error")
    else:
        agent.set_password(new_password)
        db.session.commit()
        flash(f"Password updated for {agent.display_name}.", "success")
    return redirect(url_for("admin.manage_agents"))


@bp.route("/agents/<int:agent_id>/delete", methods=["POST"])
@admin_required
def delete_agent(agent_id):
    agent = Agent.query.get_or_404(agent_id)

    if agent.id == current_user.id:
        flash("You cannot delete the account you are currently logged in as.", "error")
        return redirect(url_for("admin.manage_agents"))

    if agent.is_admin and Agent.query.filter_by(is_admin=True).count() <= 1:
        flash("Cannot delete the last remaining admin account.", "error")
        return redirect(url_for("admin.manage_agents"))

    display_name = agent.display_name
    db.session.delete(agent)  # AgentAlias rows cascade-delete with it
    db.session.commit()
    flash(f"Removed account for {display_name}. Their commission history is unaffected — only the login was removed.", "success")
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
