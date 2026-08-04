from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from agent_portal import db


class Agent(db.Model, UserMixin):
    """A portal login. One Agent account can map to one or more free-text
    `agent_name` spellings via AgentAlias (the CRM export has no stable agent
    ID, only a "Sales Rep" name string, which can vary slightly across rows)."""
    __tablename__ = "agent"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    # Nullable: an agent added by the admin without a password is Google-sign-in-only
    # (see auth.py's /login/google route). Password login is simply unavailable for
    # them until an admin sets one via the "Reset Password" form.
    password_hash = db.Column(db.String(255), nullable=True)
    display_name = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, server_default=db.false())
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    aliases = db.relationship("AgentAlias", backref="agent", lazy=True, cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if not self.password_hash:
            return False  # Google-sign-in-only account — no password to check against.
        return check_password_hash(self.password_hash, password)

    def alias_names(self):
        return [a.agent_name for a in self.aliases]


class AgentAlias(db.Model):
    """Maps one exact `agent_name` string (as it appears in the CRM export's
    "Sales Rep" column) to a portal Agent account. unique so two agents can
    never accidentally claim the same CRM name."""
    __tablename__ = "agent_alias"

    id = db.Column(db.Integer, primary_key=True)
    agent_id = db.Column(db.Integer, db.ForeignKey("agent.id"), nullable=False, index=True)
    agent_name = db.Column(db.String(255), unique=True, nullable=False, index=True)


class SyncedFile(db.Model):
    """Ledger of every Drive file drive_sync.py has already processed, so a
    daily cron run that finds no new export in the Cordoba_ADP folder is a
    no-op instead of re-importing (and erroring on) the same file."""
    __tablename__ = "synced_file"

    id = db.Column(db.Integer, primary_key=True)
    drive_file_id = db.Column(db.String(255), unique=True, nullable=False, index=True)
    drive_file_name = db.Column(db.String(255))
    drive_modified_time = db.Column(db.String(50))
    synced_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    periods_created = db.Column(db.Integer, default=0)


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

    agent_name = db.Column(db.String(255), nullable=False, index=True)
    units_cleared = db.Column(db.Integer, nullable=False)
    total_cleared_debt = db.Column(db.Float, nullable=False)
    cancellation_rate = db.Column(db.Float, nullable=False)
    hourly_draw = db.Column(db.Float, nullable=False)

    raw_tier = db.Column(db.Integer, nullable=False)
    adjusted_tier = db.Column(db.Integer, nullable=False)
    tier_rate = db.Column(db.Float, nullable=False)
    gross_commission = db.Column(db.Float, nullable=False)

    clawback_amount = db.Column(db.Float, default=0.0)
    net_commission = db.Column(db.Float, default=0.0)

    payout = db.Column(db.Float, nullable=False)
    payout_type = db.Column(db.String(20), nullable=False)

    quality_bonus_eligible = db.Column(db.Boolean, default=False)
    cancellation_penalty_applied = db.Column(db.Boolean, default=False)
    nsf_flagged = db.Column(db.Boolean, default=False)
    pending_units = db.Column(db.Integer, default=0)
    pending_debt = db.Column(db.Float, default=0.0)
    source = db.Column(db.String(20), default="drive")
    notes = db.Column(db.Text)

    clients = db.relationship("ClientRecord", backref="agent_commission", lazy=True,
                              foreign_keys="ClientRecord.agent_commission_id",
                              cascade="all, delete-orphan")


class ClientRecord(db.Model):
    """One row per client from the CRM export. Mirrors app/models.py's shape."""
    __tablename__ = "client_record"

    id = db.Column(db.Integer, primary_key=True)
    period_id = db.Column(db.Integer, db.ForeignKey("commission_period.id"), nullable=False, index=True)
    agent_commission_id = db.Column(db.Integer, db.ForeignKey("agent_commission.id"), nullable=True, index=True)

    crm_id = db.Column(db.String(50), index=True)
    agent_name = db.Column(db.String(255), index=True)
    client_name = db.Column(db.String(255))
    email = db.Column(db.String(255))
    phone = db.Column(db.String(50))
    stage = db.Column(db.String(100))
    status = db.Column(db.String(100))

    submitted_date = db.Column(db.String(50))
    enrolled_date = db.Column(db.String(50))
    first_payment_date = db.Column(db.String(50))
    first_payment_cleared_date = db.Column(db.String(50))
    second_payment_cleared_date = db.Column(db.String(50))
    dropped_date = db.Column(db.String(50))

    pay_freq = db.Column(db.String(50))
    payments_made = db.Column(db.Integer, default=0)
    nsf_count = db.Column(db.Integer, default=0)
    enrolled_debt = db.Column(db.Float, default=0.0)

    credit_score = db.Column(db.Integer, nullable=True)
    is_low_credit = db.Column(db.Boolean, default=False, server_default=db.false())

    is_cleared = db.Column(db.Boolean, default=False)
    is_pending = db.Column(db.Boolean, default=False)
    is_cancelled = db.Column(db.Boolean, default=False)

    commission_on_client = db.Column(db.Float, default=0.0)

    clawback_applied = db.Column(db.Boolean, default=False)
    clawback_period_id = db.Column(db.Integer, db.ForeignKey("commission_period.id"), nullable=True)
    clawback_amount = db.Column(db.Float, default=0.0)

    is_late_activation = db.Column(db.Boolean, default=False)
    original_cleared_period = db.Column(db.String(10), nullable=True)

    # Cordoba (funder) payout confirmation: has Cordoba's First Pays/EPF tabs ever
    # listed this client's ID? See CordobaPaidClient below.
    cordoba_paid = db.Column(db.Boolean, default=False, server_default=db.false())


class CordobaPaidClient(db.Model):
    """Ledger of every client ID that has ever appeared in a Cordoba payout file's
    First Pays or EPF tab. Kept separate from ClientRecord so a CRM upload processed
    AFTER a Cordoba file still comes in already flagged, and re-uploading the same
    weekly Cordoba file twice doesn't need special-casing."""
    __tablename__ = "cordoba_paid_client"

    id = db.Column(db.Integer, primary_key=True)
    crm_id = db.Column(db.String(50), unique=True, nullable=False, index=True)
    client_name = db.Column(db.String(255))
    source = db.Column(db.String(20))  # "first_pays" or "epf"
    uploaded_filename = db.Column(db.String(255))
    uploaded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class CordobaChargedBackClient(db.Model):
    """Ledger of every client ID that has ever triggered an ACTUAL agent commission
    deduction via a Cordoba payout file's Chargebacks tab. Kept forever so
    re-uploading the same file, or a later CRM upload reflecting the same drop,
    never claws the agent back twice."""
    __tablename__ = "cordoba_charged_back_client"

    id = db.Column(db.Integer, primary_key=True)
    crm_id = db.Column(db.String(50), unique=True, nullable=False, index=True)
    client_name = db.Column(db.String(255))
    agent_name = db.Column(db.String(255))
    clawback_amount = db.Column(db.Float, default=0.0)
    dropped_period = db.Column(db.String(10))  # YYYY-MM
    uploaded_filename = db.Column(db.String(255))
    uploaded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class CordobaChargebackMatchedClient(db.Model):
    """Ledger of every client ID from a Cordoba Chargebacks tab that has ever
    matched a client in OUR OWN commission reports — regardless of whether the
    actual dollar clawback could be applied yet. Drives the "Cordoba Clawback"
    Yes/No badge, which shows Yes as soon as the client is recognized even if the
    real deduction is still blocked (most commonly: no Dropped Date on file yet)."""
    __tablename__ = "cordoba_chargeback_matched_client"

    id = db.Column(db.Integer, primary_key=True)
    crm_id = db.Column(db.String(50), unique=True, nullable=False, index=True)
    client_name = db.Column(db.String(255))
    uploaded_filename = db.Column(db.String(255))
    uploaded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class CordobaChargebackEntry(db.Model):
    """Display-only "Cordoba Charge back" listing: a verbatim snapshot of a
    Chargebacks-tab row for every ID that matched a client in OUR OWN commission
    reports. Does NOT deduct anything from gross/net commission — purely
    informational, for the agent/owner to reconcile Cordoba's own figures by hand.
    agent_name/period_label come from OUR OWN ClientRecord (crm_id match, our own
    dropped_date), never from this file's own columns."""
    __tablename__ = "cordoba_chargeback_entry"

    id = db.Column(db.Integer, primary_key=True)
    crm_id = db.Column(db.String(50), unique=True, nullable=False, index=True)
    agent_name = db.Column(db.String(255), nullable=False, index=True)
    period_label = db.Column(db.String(10), nullable=False, index=True)  # YYYY-MM, from OUR dropped_date

    assigned_company = db.Column(db.String(255))
    enrolled_date = db.Column(db.String(50))
    client_name = db.Column(db.String(255))
    status = db.Column(db.String(100))
    marketing_payout_debt = db.Column(db.Float, default=0.0)
    first_payment_cleared_date = db.Column(db.String(50))
    pay_freq = db.Column(db.String(50))
    payments_made = db.Column(db.Integer)
    marketing_payment_cleared = db.Column(db.String(50))
    marketing_payment_chargeback = db.Column(db.String(50))
    file_dropped_date = db.Column(db.String(50))  # the FILE's own Dropped Date, display-only

    uploaded_filename = db.Column(db.String(255))
    uploaded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
