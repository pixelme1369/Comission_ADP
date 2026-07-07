from datetime import datetime, timezone
from app import db


class CommissionPeriod(db.Model):
    __tablename__ = "commission_period"

    id = db.Column(db.Integer, primary_key=True)
    period_label = db.Column(db.String(50), unique=True, nullable=False)  # YYYY-MM
    uploaded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    filename = db.Column(db.String(255))
    total_agents = db.Column(db.Integer, default=0)

    agents = db.relationship("AgentCommission", backref="period", lazy=True, cascade="all, delete-orphan")


class AgentCommission(db.Model):
    __tablename__ = "agent_commission"

    id = db.Column(db.Integer, primary_key=True)
    period_id = db.Column(db.Integer, db.ForeignKey("commission_period.id"), nullable=False)

    agent_name = db.Column(db.String(255), nullable=False)
    units_cleared = db.Column(db.Integer, nullable=False)
    total_cleared_debt = db.Column(db.Float, nullable=False)
    cancellation_rate = db.Column(db.Float, nullable=False)
    hourly_draw = db.Column(db.Float, nullable=False)

    raw_tier = db.Column(db.Integer, nullable=False)
    adjusted_tier = db.Column(db.Integer, nullable=False)
    tier_rate = db.Column(db.Float, nullable=False)
    gross_commission = db.Column(db.Float, nullable=False)

    # Clawback fields
    clawback_amount = db.Column(db.Float, default=0.0)   # total clawed back this period
    net_commission = db.Column(db.Float, default=0.0)    # gross_commission - clawback_amount (what you actually owe)

    payout = db.Column(db.Float, nullable=False)
    payout_type = db.Column(db.String(20), nullable=False)  # "commission" or "draw"

    quality_bonus_eligible = db.Column(db.Boolean, default=False)
    cancellation_penalty_applied = db.Column(db.Boolean, default=False)
    nsf_flagged = db.Column(db.Boolean, default=False)
    pending_units = db.Column(db.Integer, default=0)
    pending_debt = db.Column(db.Float, default=0.0)
    source = db.Column(db.String(20), default="manual")
    notes = db.Column(db.Text)

    clients = db.relationship("ClientRecord", backref="agent_commission", lazy=True,
                              foreign_keys="ClientRecord.agent_commission_id",
                              cascade="all, delete-orphan")


class ClientRecord(db.Model):
    """One row per client from the CRM export."""
    __tablename__ = "client_record"

    id = db.Column(db.Integer, primary_key=True)
    period_id = db.Column(db.Integer, db.ForeignKey("commission_period.id"), nullable=False)
    agent_commission_id = db.Column(db.Integer, db.ForeignKey("agent_commission.id"), nullable=True)

    # CRM identifiers
    crm_id = db.Column(db.String(50))
    agent_name = db.Column(db.String(255))
    client_name = db.Column(db.String(255))
    email = db.Column(db.String(255))
    phone = db.Column(db.String(50))
    stage = db.Column(db.String(100))
    status = db.Column(db.String(100))

    # Dates (stored as strings for display, parsed for logic)
    submitted_date = db.Column(db.String(50))
    enrolled_date = db.Column(db.String(50))
    first_payment_date = db.Column(db.String(50))
    first_payment_cleared_date = db.Column(db.String(50))
    second_payment_cleared_date = db.Column(db.String(50))
    dropped_date = db.Column(db.String(50))

    pay_freq = db.Column(db.String(50))   # "Monthly", "Biweekly", etc.
    payments_made = db.Column(db.Integer, default=0)
    nsf_count = db.Column(db.Integer, default=0)
    enrolled_debt = db.Column(db.Float, default=0.0)

    # Computed status
    is_cleared = db.Column(db.Boolean, default=False)
    is_pending = db.Column(db.Boolean, default=False)
    is_cancelled = db.Column(db.Boolean, default=False)

    # Commission earned on this specific client (enrolled_debt * period tier_rate)
    commission_on_client = db.Column(db.Float, default=0.0)

    # Clawback: if this client was cancelled in a later period and triggered a clawback
    clawback_applied = db.Column(db.Boolean, default=False)
    clawback_period_id = db.Column(db.Integer, db.ForeignKey("commission_period.id"), nullable=True)
    clawback_amount = db.Column(db.Float, default=0.0)  # amount clawed back due to this client

    # Late activation: client was pending in their cleared month, became active later
    is_late_activation = db.Column(db.Boolean, default=False)
    original_cleared_period = db.Column(db.String(10), nullable=True)  # YYYY-MM they originally cleared

    # Cordoba (funder) payout confirmation: has Cordoba's First Pays/EPF tabs ever
    # listed this client's ID? See CordobaPaidClient below.
    cordoba_paid = db.Column(db.Boolean, default=False)


class CordobaPaidClient(db.Model):
    """
    Ledger of every client ID that has ever appeared in a Cordoba payout file's First
    Pays or EPF tab. Kept separate from ClientRecord so a CRM upload processed AFTER a
    Cordoba file still comes in already flagged, and so re-uploading the same weekly
    Cordoba file twice doesn't need special-casing.
    """
    __tablename__ = "cordoba_paid_client"

    id = db.Column(db.Integer, primary_key=True)
    crm_id = db.Column(db.String(50), unique=True, nullable=False, index=True)
    client_name = db.Column(db.String(255))
    source = db.Column(db.String(20))  # "first_pays" or "epf"
    uploaded_filename = db.Column(db.String(255))
    uploaded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
