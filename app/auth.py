from functools import wraps
from flask import abort
from flask_login import login_required, current_user
from app.calculator import normalize_agent_name


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def _agent_owns(agent_commission):
    """True if the logged-in user may view this AgentCommission: an admin can view
    anyone's; an agent can only view their own (matched via normalized agent_name,
    since CRM data entry isn't perfectly consistent about casing/whitespace)."""
    if current_user.is_admin:
        return True
    return normalize_agent_name(current_user.agent_name) == normalize_agent_name(agent_commission.agent_name)
