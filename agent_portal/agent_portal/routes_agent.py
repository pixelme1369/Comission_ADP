import csv
import io

from flask import Blueprint, render_template, abort, Response
from flask_login import login_required, current_user

from agent_portal.auth import agent_scope_names
from agent_portal.models import AgentCommission, ClientRecord, CommissionPeriod

bp = Blueprint("agent", __name__, url_prefix="/portal")

CLIENT_EXPORT_COLUMNS = [
    ("crm_id", "ID"), ("client_name", "Client Name"), ("enrolled_date", "Enrolled Date"),
    ("enrolled_debt", "Enrolled Debt"), ("credit_score", "Credit Score"),
    ("commission_on_client", "Commission on Client"),
    ("first_payment_cleared_date", "1st Payment Cleared"),
    ("dropped_date", "Dropped Date"), ("payments_made", "Payments Made"),
    ("pay_freq", "Pay Freq."), ("nsf_count", "# NSF"), ("status", "Status"),
]


def _latest_period():
    """Agents only ever see the single most recent commission period (owner
    policy — no browsing prior months' history from the portal). Admins are
    unaffected; the admin dashboard still lists every period."""
    return CommissionPeriod.query.order_by(CommissionPeriod.period_label.desc()).first()


@bp.route("/")
@login_required
def dashboard():
    names = agent_scope_names()
    latest = _latest_period()
    rows = (
        AgentCommission.query
        .filter(AgentCommission.agent_name.in_(names), AgentCommission.period_id == latest.id)
        .all()
    ) if names and latest else []
    return render_template("dashboard.html", rows=rows)


def _get_scoped_agent_commission(period_id, agent_commission_id):
    names = agent_scope_names()
    latest = _latest_period()
    if not latest or period_id != latest.id:
        # Not just unscoped — genuinely not the current period. Agents can't
        # reach older periods even by guessing a URL for their own past data.
        abort(404)
    agent_row = AgentCommission.query.filter_by(id=agent_commission_id, period_id=period_id).first()
    if not agent_row or agent_row.agent_name not in names:
        abort(404)
    return agent_row


@bp.route("/period/<int:period_id>/agent/<int:agent_commission_id>")
@login_required
def period_detail(period_id, agent_commission_id):
    agent_row = _get_scoped_agent_commission(period_id, agent_commission_id)
    clients = ClientRecord.query.filter_by(agent_commission_id=agent_row.id).all()
    clawback_clients = [c for c in clients if c.clawback_applied]
    return render_template(
        "period_detail.html", agent=agent_row, period=agent_row.period,
        clients=clients, clawback_clients=clawback_clients,
    )


@bp.route("/period/<int:period_id>/agent/<int:agent_commission_id>/export")
@login_required
def export_period(period_id, agent_commission_id):
    agent_row = _get_scoped_agent_commission(period_id, agent_commission_id)
    clients = ClientRecord.query.filter_by(agent_commission_id=agent_row.id).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([label for _, label in CLIENT_EXPORT_COLUMNS])
    for c in clients:
        writer.writerow([getattr(c, field) for field, _ in CLIENT_EXPORT_COLUMNS])

    filename = f"{current_user.display_name}_{agent_row.period.period_label}.csv"
    return Response(
        output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
