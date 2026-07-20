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
    period_id = db.Column(db.Integer, db.ForeignKey("commission_period.id"), nullable=False, index=True)
    agent_commission_id = db.Column(db.Integer, db.ForeignKey("agent_commission.id"), nullable=True, index=True)

    # CRM identifiers
    # crm_id is indexed: the Cordoba chargeback flow looks clients up by it one at a
    # time, and every upload builds ID sets from it. (Indexes only apply to a freshly
    # created DB — db.create_all() doesn't alter existing tables.)
    crm_id = db.Column(db.String(50), index=True)
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

    # Credit Score (owner decision, July 2026): a client with Credit Score <= 500 who
    # clears still counts as a unit toward the agent's tier, but earns zero commission
    # dollars — see crm_parser.py's is_low_credit handling. credit_score is stored
    # purely for display/audit (why commission_on_client is $0).
    credit_score = db.Column(db.Integer, nullable=True)
    is_low_credit = db.Column(db.Boolean, default=False, server_default=db.text("0"))

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
    # server_default (not just default=) so any row inserted through a code path that
    # doesn't set this explicitly still gets a real 0, not NULL — a plain Python-side
    # default doesn't apply if the ORM model doesn't even define the column at insert
    # time, which is exactly what caused every existing row to silently end up NULL once.
    cordoba_paid = db.Column(db.Boolean, default=False, server_default=db.text("0"))


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


class CordobaChargedBackClient(db.Model):
    """
    Ledger of every client ID that has ever triggered an ACTUAL agent commission
    deduction via a Cordoba payout file's Chargebacks tab (i.e. passed every gate in
    _apply_cordoba_chargebacks: commissioned, confirmed paid, not already clawed back,
    and we have our own dropped date). Kept forever so re-uploading the same
    Chargebacks file (or a later CRM upload that reflects the same drop) never claws
    back the agent a second time for the same client. This is the money ledger —
    see CordobaChargebackMatchedClient below for the display-only ledger.
    """
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
    """
    Ledger of every client ID from a Cordoba Chargebacks tab that has ever matched a
    client in OUR OWN commission reports (ClientRecord.crm_id, any period, any
    status) — regardless of whether the actual dollar clawback could be applied yet.
    Owner policy (confirmed July 2026): the "Cordoba Clawback" Yes/No column next to
    "Cordoba Payout" on the agent detail page reflects this match immediately, even
    if the real commission deduction is still blocked on a gate in
    _apply_cordoba_chargebacks (most commonly: we don't have our own Dropped Date for
    this client yet). Kept forever, crm_id unique, so re-uploading the same file is a
    no-op and the Yes badge never disappears once shown.
    """
    __tablename__ = "cordoba_chargeback_matched_client"

    id = db.Column(db.Integer, primary_key=True)
    crm_id = db.Column(db.String(50), unique=True, nullable=False, index=True)
    client_name = db.Column(db.String(255))
    uploaded_filename = db.Column(db.String(255))
    uploaded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class CordobaChargebackEntry(db.Model):
    """
    Display-only ledger named "Cordoba Charge back" (owner request, July 2026): a
    verbatim snapshot of a Chargebacks-tab row (Assigned Company, Enrolled Date,
    Status, Marketing Payout Debt, 1st Payment Cleared Date, Pay Freq., Payments Made,
    Marketing Payment Cleared, Marketing Payment Chargeback, and the FILE'S OWN Dropped
    Date) for every ID that matched a client in OUR OWN commission reports. Unlike
    CordobaChargedBackClient, this does NOT deduct anything from the agent's
    gross/net commission and is not gated on being previously commissioned, confirmed
    paid, or not-already-clawed-back — it exists purely so the raw file row is visible
    at the bottom of the agent's commission report, for the agent/owner to reconcile
    against Cordoba's own figures by hand.

    agent_name and period_label — used only to decide WHERE to show this entry — still
    come from OUR OWN ClientRecord (crm_id match, our own dropped_date), never from
    this file's own Assigned Company / Dropped Date columns, consistent with the real
    clawback deduction path. crm_id unique so re-uploading the same Chargebacks file is
    a no-op.
    """
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
