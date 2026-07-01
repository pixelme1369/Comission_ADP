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
    cancellation_rate = db.Column(db.Float, nullable=False)  # stored as percentage, e.g. 18.5
    hourly_draw = db.Column(db.Float, nullable=False)

    raw_tier = db.Column(db.Integer, nullable=False)
    adjusted_tier = db.Column(db.Integer, nullable=False)
    tier_rate = db.Column(db.Float, nullable=False)
    gross_commission = db.Column(db.Float, nullable=False)

    payout = db.Column(db.Float, nullable=False)
    payout_type = db.Column(db.String(20), nullable=False)  # "commission" or "draw"

    quality_bonus_eligible = db.Column(db.Boolean, default=False)
    cancellation_penalty_applied = db.Column(db.Boolean, default=False)
    nsf_flagged = db.Column(db.Boolean, default=False)       # any client with # NSF >= 3
    pending_units = db.Column(db.Integer, default=0)         # units held due to Pending Affiliate Cancellation
    pending_debt = db.Column(db.Float, default=0.0)          # enrolled debt of pending units
    source = db.Column(db.String(20), default="manual")      # "manual" or "crm"
    notes = db.Column(db.Text)
