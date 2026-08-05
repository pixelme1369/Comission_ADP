"""
Parses a full-history CRM export (one row per client, all months in one file).

Shared between app/ (the internal single-user tool) and agent_portal/ (the
multi-user portal) — see commission_core/README.md for why this package
exists and physically lives where it does. The two apps' calculated
commission/clawback numbers are IDENTICAL except for two owner-confirmed,
explicit divergences, both controlled by keyword-only flags on
parse_crm_and_calculate() rather than forked copies of this file:

1. persist_same_month_cancel (default False, matching app/'s original
   behavior; agent_portal passes True): when True, clients who dropped the
   same month they cleared (or before their own payout date) are also
   surfaced as display-only rows in each period's client list, so agents can
   see who dropped without any commission ever being paid on them. This
   never touches units_cleared/total_cleared_debt/cancellation_rate/
   gross_commission either way — purely a display list. app/ has never
   shown this list at all; agent_portal added it deliberately.

2. require_prior_payment_evidence (default True, matching app/'s original/
   still-current policy; agent_portal passes False): governs two related
   mechanisms that both hinge on "do we have proof this portal already paid
   the agent for this client, from EARLIER data in our own database":
     - Late activation: reassigning a client's commission credit forward to
       the latest period in the file when they're currently cleared but
       their crm_id was never seen as paid before (see the block below).
     - The Step 3 clawback guard: refusing to claw back a "clawback"-
       classified client unless they were either in this file's own cleared
       bucket or already recorded as paid in the database.
   True (app/'s policy): both run — a client with no prior-payment evidence
   in the file/DB is NOT trusted as a genuine late activation or clawback.
   False (agent_portal's policy, owner-confirmed August 2026 — see
   "always assume i paid them for previous cleared files" in the commit
   history): a client's own 1st Payment Cleared Date on the row IS treated
   as proof enough on its own — every client is always credited in their own
   real cleared_period (no late-activation reassignment at all), and every
   clawback-classified row is always clawed back (no proof-of-payment guard)
   regardless of whether this portal's own upload history happens to be
   complete. This was an explicit, repeated owner directive for agent_portal
   specifically; app/ was deliberately left on the older, more conservative
   policy per owner sign-off during the commission_core merge (August 2026)
   rather than silently unifying the money math.

3. already_history_paid_crm_ids (default None/empty set; app/ never passes
   this — see below for why it's structurally impossible for app/ to need
   it — agent_portal passes every crm_id already recorded as paid via a
   separate Commission History import for that exact client): owner policy,
   confirmed August 2026 — "This file has already been paid. Don't
   calculate it again. Only watch it going forward to see if it drops and
   needs a clawback." A full CRM export re-includes every client ever
   cleared, including ones from before agent_portal existed that Commission
   History already backfilled. Without this, the FIRST live CRM upload
   after a History backfill would re-credit those same clients a second
   time as a brand-new "cleared" (or "safe_cancel") unit in a fresh
   calculated period for the same month they were already paid for —  a
   real double payment, not a hypothetical. A row whose crm_id is in this
   set is skipped entirely at classification time (Step 1) when it would
   otherwise count as "cleared" or "safe_cancel" — it contributes no unit,
   no debt, no commission dollars to any period, exactly as if it weren't
   in the file at all. This does NOT touch clawback detection: a row is
   only ever skipped here while it's still active (no Dropped Date); the
   moment a future upload shows that same crm_id with cleared+dropped dates
   together, it classifies as "clawback" (or same_month_cancel/safe_cancel)
   from the row's own dates same as any other client, completely
   independent of this set — "watch it going forward" already works with
   zero additional logic, since clawback classification never consulted
   this set to begin with. Structurally impossible for app/ to need this:
   app/ never relaxed its own "period already exists" guard (see
   agent_portal/README.md and CLAUDE.md), so app/ can never have a
   Commission History period and a calculated period coexist for the same
   month in the first place — there is nothing for this set to protect
   against there. Passing None (app/'s call sites) is a complete no-op.

Both apps get the Step 3.5 visibility-fix pass (see below) UNCONDITIONALLY,
regardless of either flag — it has zero effect on any dollar amount, and
fixes a latent "client silently vanishes from every page" display bug that
existed under app/'s original policy just as much as agent_portal's (a
solo reclassified client with no other activity in their own cleared period
never got a result entry to attach their ClientRecord to). This is the one
behavior change this merge makes to app/'s output versus its pre-merge code:
a client who used to vanish now shows up (still $0, still no clawback) in
the period they belong to. No calculated commission or clawback dollar
amount for any agent changes.

For each client row, the row-level classification is identical in every
build:
  - If 1st Payment Cleared Date filled + Dropped Date empty + Status != Pending Affiliate Cancellation
    → CLEARED: counts as a unit in the cleared month, commission is owed

  - If 1st Payment Cleared Date filled + Dropped Date filled + same month
    → SAME_MONTH_CANCEL: excluded from commission, NOT a clawback (never paid)

  - If 1st Payment Cleared Date filled + Dropped Date filled + different month + Payments Made
    hits the safe threshold for their Pay Freq. (Monthly=2, Biweekly=4, unknown=3) before dropping
    → SAFE_CANCEL: no clawback, regardless of whether the drop happened before or after the payout
    date. Still counts as a full unit toward the agent's tier for that month (owner policy, July
    2026) — but earns $0 commission and is excluded from the cancellation-rate denominator, same
    "unit credited, no dollars" treatment as a Credit Score <= 500 client (see below).

  - If 1st Payment Cleared Date filled + Dropped Date filled + different month + never hit the safe
    threshold + dropped before the 25th payout date
    → SAME_MONTH_CANCEL: commission was never sent, excluded, not a clawback

  - If 1st Payment Cleared Date filled + Dropped Date filled + different month + never hit the safe
    threshold + dropped on/after the payout date
    → CLAWBACK: commission was paid in the cleared month, must be deducted from the client's own
    Dropped Date month (see Step 3 below)

  - If 1st Payment Cleared Date filled + Status == Pending Affiliate Cancellation
    → PENDING: not paid yet

Clawbacks are computed entirely within the parser (no DB lookups needed) since
the full history is in one file.

Credit Score (optional column, owner decision July 2026): a client who clears with
Credit Score <= 500 still counts as a full unit toward the agent's tier, but earns
zero commission dollars — their debt is excluded from the period's total_cleared_debt
entirely, individually and in aggregate. Older CRM exports without this column are
unaffected (missing/unparseable Credit Score is just treated as "not low credit").
This replaced an earlier mechanism (matching Cordoba's separate EPF-tab payout file)
that was fragile to upload ordering — Credit Score lives in the CRM row itself, so
there's nothing to reconcile across files.
"""

import csv
import io
from collections import defaultdict
from datetime import datetime

from commission_core.calculator import calculate_agent_commission, calculate_clawback_amount, get_fixed_rate

NSF_FLAG_THRESHOLD = 3

# Minimum payments before clawback protection kicks in, by payment frequency
def _safe_payment_threshold(pay_freq: str) -> int:
    """Return the number of payments that protects against clawback."""
    # Normalize "Bi-Weekly" (the actual CRM export spelling) and "Biweekly" alike —
    # a hyphen-sensitive match here previously fell through to the unknown/missing
    # fallback (3) for every real Bi-Weekly row instead of the intended 4.
    freq = (pay_freq or "").strip().lower().replace("-", "").replace(" ", "")
    if freq in ("biweekly", "semimonthly"):
        return 4
    if freq == "monthly":
        return 2
    return 3  # fallback for unknown / missing (old files)

CRM_REQUIRED_COLUMNS = {
    "sales rep",
    "1st payment cleared date",
    "dropped date",
    "status",
    "enrolled debt",
    "# nsf",
}


def _parse_date(value: str):
    value = value.strip()
    if not value:
        return None
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _period_of(dt) -> str:
    return dt.strftime("%Y-%m") if dt else None


def _payment_date_for_period(period_str: str):
    """Commission for a cleared period is paid on the 25th of the following month."""
    if not period_str:
        return None
    dt = datetime.strptime(period_str, "%Y-%m")
    # Advance one month
    if dt.month == 12:
        return datetime(dt.year + 1, 1, 25)
    return datetime(dt.year, dt.month + 1, 25)


def _parse_currency(value: str) -> float:
    return float(value.strip().replace("$", "").replace(",", "") or 0)


def _zero_unit_holding_result(agent_name: str) -> dict:
    """A $0/0-unit commission result shape, for a period where an agent has
    no genuinely cleared or safe-cancel unit but still needs SOME result
    entry to attach client records to (a clawback landing in a period with
    no other activity — see Step 4 — or a client record that needs to stay
    visible after Step 3 reclassifies it — see the pass right after Step 3).
    calculate_agent_commission() rejects units_cleared=0 (tiers start at 1),
    so this is built by hand instead. Every value here is 0/empty — this
    cannot change what any agent is paid, only what shows up in the UI."""
    return {
        "agent_name": agent_name,
        "units_cleared": 0,
        "total_cleared_debt": 0.0,
        "cancellation_rate": 0.0,
        "hourly_draw": 0.0,
        "raw_tier": 0,
        "adjusted_tier": 0,
        "tier_rate": 0.0,
        "gross_commission": 0.0,
        "clawback_amount": 0.0,
        "net_commission": 0.0,
        "payout": 0.0,
        "payout_type": "none",
        "quality_bonus_eligible": False,
        "cancellation_penalty_applied": False,
        "nsf_flagged": False,
        "pending_units": 0,
        "pending_debt": 0.0,
        "source": "crm",
        "notes": "",
        "_cleared_clients": [],
        "_all_period_clients": [],
    }


def parse_crm_and_calculate(file_bytes: bytes, filename: str, already_cleared_crm_ids: set = None,
                             already_charged_back_crm_ids: set = None,
                             already_low_credit_crm_ids: set = None,
                             already_history_paid_crm_ids: set = None,
                             *,
                             persist_same_month_cancel: bool = False,
                             require_prior_payment_evidence: bool = True) -> list:
    """
    Parse a full-history CRM export and return one dict per commission period found.

    persist_same_month_cancel, require_prior_payment_evidence, and
    already_history_paid_crm_ids are the three owner-confirmed, app-specific
    policy divergences described in the module docstring above — see there
    before changing any of them.

    already_charged_back_crm_ids (owner policy, confirmed August 2026): must
    contain every crm_id that already has ANY ClientRecord with
    clawback_applied=True — not just Cordoba-sourced ones. This changed
    meaning when clawback placement moved from "latest period in file" to
    "the client's own Dropped Date month" (see Step 3): that target period
    is now stable/unchanging for a given client across every future upload
    (their Dropped Date never changes), so this set is the ONLY thing
    preventing the same client from being re-clawed-back on every single
    later upload that still contains their row — which every full-history
    CRM export always will. Each app's ingest layer is responsible for
    building this set from its own ClientRecord table (both Cordoba's own
    ledger AND every clawback_applied=True row, regardless of what created
    it) — see ingest.py/routes.py's already_known_crm_id_sets()-equivalent
    for the query.

    Returns list of:
    {
        "period_label": "2026-05",
        "filename": str,
        "results": [ agent_result_dict, ... ],
        "client_rows": [ client_row_dict, ... ],
        "errors": [],
    }
    """
    errors = []
    if already_low_credit_crm_ids is None:
        already_low_credit_crm_ids = set()
    if already_history_paid_crm_ids is None:
        already_history_paid_crm_ids = set()

    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [{"errors": ["File must be UTF-8 encoded."], "period_label": None,
                 "filename": filename, "results": [], "client_rows": []}]

    reader = csv.DictReader(io.StringIO(text))

    if not reader.fieldnames:
        return [{"errors": ["CSV file is empty or has no header row."], "period_label": None,
                 "filename": filename, "results": [], "client_rows": []}]

    actual_cols = {c.strip().lower() for c in reader.fieldnames if c}
    missing = CRM_REQUIRED_COLUMNS - actual_cols
    if missing:
        return [{"errors": [f"Missing required CRM columns: {', '.join(sorted(missing))}"],
                 "period_label": None, "filename": filename, "results": [], "client_rows": []}]

    col_map = {c.strip().lower(): c for c in reader.fieldnames if c}

    def get(row, key):
        return row.get(col_map.get(key, key), "").strip()

    # Parse every row first
    all_clients = []
    row_errors = []

    for row_num, raw_row in enumerate(reader, start=2):
        agent = get(raw_row, "sales rep")
        if not agent:
            row_errors.append(f"Row {row_num}: missing Sales Rep, skipped")
            continue

        cleared_date = _parse_date(get(raw_row, "1st payment cleared date"))
        dropped_date = _parse_date(get(raw_row, "dropped date"))
        status = get(raw_row, "status")

        try:
            enrolled_debt = _parse_currency(get(raw_row, "enrolled debt"))
        except ValueError:
            enrolled_debt = 0.0
            row_errors.append(f"Row {row_num} ({agent}): invalid Enrolled Debt, using 0")

        try:
            nsf_count = int(get(raw_row, "# nsf") or 0)
        except ValueError:
            nsf_count = 0

        try:
            payments_made = int(get(raw_row, "payments made") or 0)
        except ValueError:
            payments_made = 0

        pay_freq = get(raw_row, "pay freq.")
        if not pay_freq.strip():
            row_errors.append(f"Row {row_num} ({agent}): Pay Freq. is blank — clawback threshold defaulted to 3, please review")
        safe_threshold = _safe_payment_threshold(pay_freq)

        is_pending_cancellation = status.strip().lower() == "pending affiliate cancellation"
        cleared_period = _period_of(cleared_date)
        dropped_period = _period_of(dropped_date)
        same_month = (cleared_period and dropped_period and cleared_period == dropped_period)

        # Commission for the cleared month is paid on the 25th of the FOLLOWING month.
        # If the client drops BEFORE that payment date, commission was never sent out
        # → treat as a non-paying cancel (same_month_cancel bucket), NOT a clawback.
        payment_date = _payment_date_for_period(cleared_period)
        dropped_before_payment = (
            dropped_date and payment_date and dropped_date < payment_date
        )

        # Classify the client
        if cleared_date and not dropped_date and not is_pending_cancellation:
            unit_status = "cleared"
        elif cleared_date and not dropped_date and is_pending_cancellation:
            if payments_made >= safe_threshold:
                unit_status = "cleared"  # safe threshold reached — commission protected even if cancelled
            else:
                unit_status = "pending"
        elif cleared_date and dropped_date and same_month:
            unit_status = "same_month_cancel"
        elif cleared_date and dropped_date and not same_month and payments_made >= safe_threshold:
            # Enough payments already cleared before the drop — safe regardless of payout-date timing
            unit_status = "safe_cancel"
        elif cleared_date and dropped_date and not same_month and dropped_before_payment:
            # Dropped before the 25th payout, never hit the safe threshold — commission was never sent, just exclude
            unit_status = "same_month_cancel"
        elif cleared_date and dropped_date and not same_month:
            unit_status = "clawback"
        else:
            unit_status = "not_yet_cleared"
            if not cleared_date:
                continue  # no cleared date = no commission relevance

        crm_id = get(raw_row, "id")

        # Credit Score (owner decision, July 2026): optional column. A client who
        # clears with Credit Score <= 500 still counts as a full unit toward the
        # agent's tier, but earns zero commission dollars — see the debt/commission
        # exclusion in Step 2 below. Missing/unparseable Credit Score is just treated
        # as "not low credit" (no error) since older CRM exports won't have this column.
        credit_score_raw = get(raw_row, "credit score")
        credit_score = None
        if credit_score_raw:
            try:
                credit_score = int(float(credit_score_raw))
            except ValueError:
                credit_score = None
        # Not gated on unit_status: a client whose single CRM row already shows both
        # a cleared and dropped date (classified "clawback" outright, not "cleared"
        # then later dropped) still needs this flag so Step 3's clawback guard can
        # see it — the credit score reflects the client, not the row's classification.
        is_low_credit = credit_score is not None and credit_score <= 500

        all_clients.append({
            "crm_id": crm_id,
            "agent_name": agent,
            "client_name": get(raw_row, "full name"),
            "email": get(raw_row, "email"),
            "phone": get(raw_row, "home phone"),
            "stage": get(raw_row, "stage"),
            "status": status,
            "submitted_date": get(raw_row, "submitted date"),
            "enrolled_date": get(raw_row, "enrolled date"),
            "first_payment_date": get(raw_row, "1st payment date"),
            "first_payment_cleared_date": get(raw_row, "1st payment cleared date"),
            "second_payment_cleared_date": get(raw_row, "2nd payment cleared date"),
            "dropped_date": get(raw_row, "dropped date"),
            "pay_freq": pay_freq,
            "payments_made": payments_made,
            "nsf_count": nsf_count,
            "enrolled_debt": enrolled_debt,
            "credit_score": credit_score,
            "is_low_credit": is_low_credit,
            "unit_status": unit_status,
            "cleared_period": cleared_period,
            "dropped_period": dropped_period,
            "is_cleared": unit_status == "cleared",
            "is_pending": unit_status == "pending",
            "is_cancelled": dropped_date is not None,
            "commission_on_client": 0.0,   # filled in below
            "clawback_amount": 0.0,        # filled in below
        })

    # ---------------------------------------------------------------
    # Late activation: gated behind require_prior_payment_evidence (see the
    # module docstring). When True (app/'s policy): if a client is currently
    # cleared (active) but their crm_id was never saved as is_cleared in the
    # DB, and their cleared_period is earlier than the latest period in this
    # file, they were pending before and just became active — credit their
    # commission in the latest period instead of their cleared month. When
    # False (agent_portal's policy): skipped entirely — every client is
    # always credited in their own real cleared_period, no reassignment,
    # since a real cleared date on the row is treated as proof enough on its
    # own that the agent was paid, regardless of what this portal's own
    # upload history happens to show.
    #
    # Only meaningful when already_cleared_crm_ids reflects real prior
    # history. On a brand-new database (first-ever upload, or right after
    # a schema-change wipe) that set is empty, and this heuristic can't
    # distinguish "genuinely cleared last month" from "was pending, just
    # went active" — it would misclassify EVERY older client in a
    # multi-month full-history file as a late activation and collapse
    # their commission into the most recent month. Skip it entirely in
    # that case; every client is simply credited in their own cleared month.
    # ---------------------------------------------------------------
    if already_cleared_crm_ids is None:
        already_cleared_crm_ids = set()
    if already_charged_back_crm_ids is None:
        already_charged_back_crm_ids = set()

    # The latest cleared-month found anywhere in this file — used for late
    # activation only (above, when enabled). No longer used for clawback
    # placement (see Step 3: clawbacks now target the client's own Dropped
    # Date month, owner policy confirmed August 2026).
    all_cleared_periods = [c["cleared_period"] for c in all_clients if c["cleared_period"]]
    latest_period_in_file = max(all_cleared_periods) if all_cleared_periods else None

    if require_prior_payment_evidence and already_cleared_crm_ids:
        latest_period = latest_period_in_file

        for c in all_clients:
            if (
                c["unit_status"] == "cleared"
                and c["crm_id"]
                and c["crm_id"] not in already_cleared_crm_ids
                and c["cleared_period"]
                and latest_period
                and c["cleared_period"] < latest_period
            ):
                c["original_cleared_period"] = c["cleared_period"]
                c["cleared_period"] = latest_period
                c["is_late_activation"] = True

    # ---------------------------------------------------------------
    # Step 1: Build per-agent per-period cleared unit counts
    # (agent, cleared_period) → list of cleared clients
    # ---------------------------------------------------------------
    cleared_buckets = defaultdict(list)      # (agent, period) → cleared clients
    cancel_buckets = defaultdict(list)       # (agent, period) → cancelled clients (for cancel rate)
    pending_buckets = defaultdict(list)      # (agent, period) → pending clients
    safe_cancel_buckets = defaultdict(list)  # (agent, period) → safe-cancel clients
    # same_month_cancel_buckets is always built (cheap) — whether it actually
    # reaches any output depends on persist_same_month_cancel below, in Step 2
    # and Step 3.5. Never merged into any bucket that feeds units/debt/rate/
    # commission, regardless of the flag.
    same_month_cancel_buckets = defaultdict(list)  # (agent, period) → same-month-cancel clients
    # Rows skipped below because already_history_paid_crm_ids says this exact
    # client was already paid via a separate Commission History import — kept
    # only to power a single summary warning, not a per-client one; see the
    # module docstring's already_history_paid_crm_ids section for why this
    # exclusion exists and why it's safe.
    already_paid_skipped = []

    for c in all_clients:
        key = (c["agent_name"], c["cleared_period"])
        already_history_paid = bool(c["crm_id"]) and c["crm_id"] in already_history_paid_crm_ids
        if c["unit_status"] == "cleared":
            if already_history_paid:
                already_paid_skipped.append(c)
                continue
            cleared_buckets[key].append(c)
        elif c["unit_status"] == "same_month_cancel":
            same_month_cancel_buckets[key].append(c)
        elif c["unit_status"] == "safe_cancel":
            # OWNER POLICY (confirmed July 2026): a safe_cancel client protected the
            # agent's commission (enough payments landed before they dropped), but the
            # client's own dollars don't belong in the payout — same "unit credited,
            # $0 commission" treatment Credit Score gets (see Step 2). Kept in its own
            # bucket rather than merged into cleared_buckets so the cancellation-rate
            # denominator below still excludes them, per the locked cancel-rate policy.
            if already_history_paid:
                already_paid_skipped.append(c)
                continue
            safe_cancel_buckets[key].append(c)
        elif c["unit_status"] == "clawback":
            # Only clawback clients count toward cancel rate
            # same_month_cancel and safe_cancel are excluded.
            #
            # POLICY (confirmed by owner, July 2026): a client counted here stays in the
            # cancellation rate even if Step 3 later determines the agent was never paid
            # on them (pending → cancelled) and charges no clawback. An enrolled client
            # who cancelled counts against the agent's quality rate regardless of whether
            # commission ever went out. Do NOT "fix" this by excluding them from the rate.
            cancel_buckets[key].append(c)
        elif c["unit_status"] == "pending":
            pending_buckets[key].append(c)

    if already_paid_skipped:
        names = ", ".join(
            f"{c.get('client_name') or c.get('crm_id')} ({c['agent_name']}, {c['cleared_period']})"
            for c in already_paid_skipped[:10]
        )
        more = f" and {len(already_paid_skipped) - 10} more" if len(already_paid_skipped) > 10 else ""
        row_errors.append(
            f"{len(already_paid_skipped)} client(s) already paid via a Commission History import — "
            f"not recalculated (still watched for a future clawback if they drop): {names}{more}"
        )

    # ---------------------------------------------------------------
    # Step 2: Calculate base commission per agent per cleared period
    # Store (agent, period) → commission result dict
    # ---------------------------------------------------------------
    agent_period_results = {}  # (agent, cleared_period) → result dict

    # Union of keys, since an agent/period can have safe-cancel units with no
    # ordinary "cleared" client at all (e.g. everyone who cleared that month later
    # dropped, but stayed protected by the safe-payment threshold).
    tier_keys = set(cleared_buckets.keys()) | set(safe_cancel_buckets.keys())

    for agent_name, period_label in tier_keys:
        key = (agent_name, period_label)
        cleared = cleared_buckets.get(key, [])
        safe_cancels = safe_cancel_buckets.get(key, [])
        cancelled = cancel_buckets.get(key, [])
        pending = pending_buckets.get(key, [])
        same_month_cancels = same_month_cancel_buckets.get(key, []) if persist_same_month_cancel else []

        # OWNER POLICY (confirmed July 2026): safe-cancel clients still count as a full
        # unit toward the agent's tier (same "unit credited, $0 commission" treatment as
        # a Credit Score <= 500 client below) even though they later dropped — the agent
        # already earned the protection by hitting the safe payment threshold.
        tier_units = cleared + safe_cancels
        units_cleared = len(tier_units)
        # Credit Score <= 500 clients (owner policy, July 2026) still count as a full
        # unit toward the agent's tier, but their debt is excluded from the dollar
        # basis entirely — they earn zero commission, individually or in aggregate.
        low_credit_clients = [c for c in tier_units if c["is_low_credit"]]
        total_cleared_debt = sum(
            c["enrolled_debt"] for c in tier_units
            if not c["is_low_credit"] and c["unit_status"] != "safe_cancel"
        )
        # Cancel rate = clawback clients / (cleared + clawback clients)
        # Same-month cancels, safe cancels, and pending are excluded from both sides —
        # so the rate denominator uses only true "cleared" clients, not safe_cancels.
        # Low-credit clients are still real ClientRecords/units, so they stay included
        # on the cleared side same as any other cleared client.
        total_for_rate = len(cleared) + len(cancelled)
        cancel_rate_pct = (len(cancelled) / total_for_rate * 100) if total_for_rate > 0 else 0.0
        nsf_flagged = any(c["nsf_count"] >= NSF_FLAG_THRESHOLD
                          for c in tier_units + cancelled + pending)

        # units_cleared is always >= 1 here — key came from cleared_buckets or
        # safe_cancel_buckets, both of which only gain a key when something is
        # appended to it.
        result = calculate_agent_commission(
            agent_name=agent_name,
            units_cleared=units_cleared,
            total_cleared_debt=total_cleared_debt,
            cancellation_rate_pct=cancel_rate_pct,
            hourly_draw=0.0,
        )
        result["clawback_amount"] = 0.0
        result["net_commission"] = result["gross_commission"]
        result["nsf_flagged"] = nsf_flagged
        result["pending_units"] = len(pending)
        result["pending_debt"] = sum(c["enrolled_debt"] for c in pending)
        result["source"] = "crm"
        result["_cleared_clients"] = tier_units
        # same_month_cancels is display-only (see persist_same_month_cancel above) —
        # it never touches units_cleared, total_cleared_debt, cancellation_rate, or
        # gross_commission either way.
        result["_all_period_clients"] = tier_units + cancelled + pending + same_month_cancels

        if len(pending) > 0:
            result["notes"] += f" | {len(pending)} unit(s) pending Affiliate Cancellation review"
        if nsf_flagged:
            result["notes"] += f" | NSF flag: client(s) with {NSF_FLAG_THRESHOLD}+ NSF events"
        if low_credit_clients:
            result["notes"] += (
                f" | {len(low_credit_clients)} unit(s) counted at $0 commission "
                "(Credit Score <= 500)"
            )
        if safe_cancels:
            result["notes"] += (
                f" | {len(safe_cancels)} unit(s) counted at $0 commission "
                "(safe cancel — payment threshold met before drop)"
            )

        # Note any late activations included in this period
        late_activations = [c for c in tier_units if c.get("is_late_activation")]
        if late_activations:
            periods = sorted({c["original_cleared_period"] for c in late_activations})
            result["notes"] += (
                f" | {len(late_activations)} late activation(s) — originally cleared "
                f"{', '.join(periods)}, commission credited this period"
            )

        # Commission per cleared client — zero for low-credit and safe-cancel clients
        for c in tier_units:
            c["commission_on_client"] = (
                0.0 if (c["is_low_credit"] or c["unit_status"] == "safe_cancel")
                else round(c["enrolled_debt"] * result["tier_rate"], 2)
            )

        agent_period_results[(agent_name, period_label)] = result

    # ---------------------------------------------------------------
    # Step 3: Calculate clawbacks
    # For each clawback client, find their original cleared period,
    # recalculate that period's commission without them, compute delta.
    #
    # OWNER POLICY (confirmed August 2026, supersedes the "latest period in
    # file" rule this used to be — see git history / CLAUDE.md for that
    # policy's own reasoning while it was in effect): the deduction is booked
    # against the client's own Dropped Date month — not the latest period
    # found anywhere in the file. Rationale: for payroll/accounting purposes,
    # a clawback needs to be traceable to the real event that caused it
    # (this client dropped in THIS month), not floated onto whatever the
    # newest month in an unrelated upload happens to be. This is the same
    # placement rule the separate Cordoba-chargeback clawback path has
    # always used (routes.py/cordoba_ingest.py's _get_or_create_agent_period_row)
    # — the two paths are now consistent with each other.
    #
    # Because the target period is now STABLE per client (a client's own
    # Dropped Date never changes across re-uploads, unlike "latest period in
    # file" which shifted forward every time), already_charged_back_crm_ids
    # below is the ONLY thing standing between a client and being re-clawed-
    # back on every single future upload that still contains their row (which
    # full-history CRM exports always do). Callers MUST feed this set from
    # every ClientRecord.clawback_applied=True crm_id, not just Cordoba's own
    # ledger — see the parameter's own note in the function signature above
    # and each app's ingest layer for how it's built.
    # ---------------------------------------------------------------
    # (agent, target_period) → list of (client, clawback_amount)
    clawback_by_target_period = defaultdict(list)
    # Clients reclassified out of "clawback" below (no proof of prior payment
    # under require_prior_payment_evidence, or low-credit) — tracked so the
    # visibility pass right after this loop can make sure they still show up
    # somewhere. See that pass for why.
    reclassified_clients = []

    for c in all_clients:
        if c["unit_status"] != "clawback":
            continue

        crm_id = c.get("crm_id", "")
        agent_name = c["agent_name"]
        cleared_period = c["cleared_period"]
        target_period = c["dropped_period"]
        orig_key = (agent_name, cleared_period)

        if crm_id and crm_id in already_charged_back_crm_ids:
            # Already clawed back — either via a Cordoba Chargebacks-tab
            # upload, or via a PRIOR CRM upload's own clawback detection (see
            # the note above this loop) — don't double-charge the agent for
            # the same client twice.
            continue

        # Proof-of-payment guard — gated behind require_prior_payment_evidence
        # (see the module docstring). When True (app/'s policy): a "clawback"
        # row is reclassified to a non-paying same_month_cancel (no deduction)
        # unless the client also appeared in THIS file's cleared bucket or had
        # a prior DB record of is_cleared=True — i.e. we need proof the agent
        # was ever actually paid before clawing anything back. When False
        # (agent_portal's policy): skipped — a real 1st Payment Cleared Date
        # on the row IS treated as proof enough on its own, so every such row
        # is always clawed back regardless of this portal's own upload
        # history. Either way, only two things still block a deduction:
        # already clawed back via Cordoba (above), and low-credit clients,
        # who were never paid anything to claw back regardless (below).
        if require_prior_payment_evidence:
            was_cleared_in_file = any(
                x.get("crm_id") == crm_id
                for x in cleared_buckets.get(orig_key, [])
            )
            was_paid_in_db = bool(crm_id and crm_id in already_cleared_crm_ids)

            if not was_cleared_in_file and not was_paid_in_db:
                # Commission was never paid (e.g. client was pending then cancelled).
                # Reclassify as a non-paying cancel — no clawback applies.
                c["unit_status"] = "same_month_cancel"
                c["is_cancelled"] = True
                reclassified_clients.append(c)
                continue

        # Guard: a Credit Score <= 500 client earns zero commission when they clear
        # (see Step 2) — there's nothing to claw back if they later drop, even though
        # they're technically "cleared" and counted toward the tier. c["is_low_credit"]
        # covers a single row that already shows both cleared+dropped dates (classified
        # "clawback" outright, never passing through the cleared bucket at all);
        # already_low_credit_crm_ids covers a client who cleared low-credit in a PRIOR
        # upload and this row doesn't (or can't) repeat their Credit Score.
        if c.get("is_low_credit") or (crm_id and crm_id in already_low_credit_crm_ids):
            c["unit_status"] = "same_month_cancel"
            c["is_cancelled"] = True
            reclassified_clients.append(c)
            continue

        orig_result = agent_period_results.get(orig_key)
        if not orig_result:
            # Commission record not found (agent had 0 cleared in that month after cancels)
            # Clawback = just this client's debt × lowest possible rate (or the agent's
            # contractual fixed rate, if they have one)
            fallback_rate = get_fixed_rate(agent_name) or 0.01
            cb = round(c["enrolled_debt"] * fallback_rate, 2)
            c["clawback_amount"] = cb
            clawback_by_target_period[(agent_name, target_period)].append(c)
            continue

        cb = calculate_clawback_amount(
            orig_result["units_cleared"],
            orig_result["total_cleared_debt"],
            orig_result["gross_commission"],
            orig_result["cancellation_rate"],
            c["enrolled_debt"],
            agent_name=agent_name,
        )
        c["clawback_amount"] = cb
        clawback_by_target_period[(agent_name, target_period)].append(c)

    # ---------------------------------------------------------------
    # Step 3.5: BUGFIX — visibility for clients just reclassified above.
    # Applies UNCONDITIONALLY regardless of either policy flag (see the
    # module docstring) — it has zero effect on any dollar amount.
    #
    # A client reclassified from "clawback" to "same_month_cancel" (no proof
    # of prior payment, or low-credit) was only ever added to cancel_buckets
    # during the initial classification pass, never cleared_buckets or
    # safe_cancel_buckets. If that was the ONLY activity for their agent in
    # their own cleared period, Step 2's tier_keys never included that
    # (agent, period) at all — no result entry was created, so the client's
    # record has nowhere to attach and silently vanishes from EVERY page,
    # with zero trace. Reproduced: a single-row file shaped exactly like this
    # (cleared one month, dropped a later month, no prior upload/backfill to
    # prove the agent was ever paid) returned "No commissionable rows found
    # in file" even though the row parsed and classified correctly.
    #
    # If some OTHER client already gave this (agent, period) a result entry
    # in Step 2, there's nothing to do — cancel_buckets holds a reference to
    # the same dict Step 2 already read into that entry's _all_period_clients,
    # so the reclassification above is already reflected there. This only
    # ever creates a $0/0-unit holding entry for a client whose own row was
    # never worth anything anyway (the whole reason they were reclassified);
    # it cannot change any dollar amount for any agent.
    # ---------------------------------------------------------------
    for c in reclassified_clients:
        key = (c["agent_name"], c["cleared_period"])
        if key in agent_period_results:
            continue
        pending = pending_buckets.get(key, [])
        # Only the reclassified sibling(s) sharing this key — a genuine
        # (non-reclassified) clawback client sharing the same key deliberately
        # stays invisible at their OWN cleared period (they show up at their
        # Dropped Date month instead — see Step 3/4 above); this must not
        # resurrect them here.
        cancelled = [x for x in cancel_buckets.get(key, []) if x["unit_status"] != "clawback"]
        result = _zero_unit_holding_result(c["agent_name"])
        result["pending_units"] = len(pending)
        result["pending_debt"] = sum(x["enrolled_debt"] for x in pending)
        same_month_cancels = same_month_cancel_buckets.get(key, []) if persist_same_month_cancel else []
        result["_all_period_clients"] = cancelled + pending + same_month_cancels
        agent_period_results[key] = result

    # ---------------------------------------------------------------
    # Step 4: Apply clawbacks to the target period's results (the client's
    # own Dropped Date month — see Step 3). If no commission result exists
    # there yet, create a zero-unit entry just to carry the clawback.
    # ---------------------------------------------------------------
    for (agent_name, target_period), cb_clients in clawback_by_target_period.items():
        total_cb = round(sum(c["clawback_amount"] for c in cb_clients), 2)
        key = (agent_name, target_period)

        if key not in agent_period_results:
            # Agent had no cleared units in the target period — create a holding entry
            agent_period_results[key] = _zero_unit_holding_result(agent_name)

        r = agent_period_results[key]
        r["clawback_amount"] = round(r.get("clawback_amount", 0.0) + total_cb, 2)
        r["net_commission"] = max(0.0, round(r["gross_commission"] - r["clawback_amount"], 2))
        r["notes"] = (r.get("notes") or "") + \
            f" | Clawback -${total_cb:,.2f} from {len(cb_clients)} previously-paid cancelled client(s)"
        r["_clawback_clients"] = cb_clients

    # ---------------------------------------------------------------
    # Step 5: Group everything by period for output
    # ---------------------------------------------------------------
    period_map = defaultdict(list)
    for (agent_name, period_label), result in agent_period_results.items():
        result["_period_label"] = period_label
        period_map[period_label].append(result)

    # Also collect agents with only clawbacks in a period (no cleared units there)
    # already handled above via the holding entry

    periods_out = []
    for period_label in sorted(period_map.keys()):
        agent_results = period_map[period_label]

        # Build client_rows for this period
        period_client_rows = []
        for r in agent_results:
            for c in r.get("_all_period_clients", []):
                c["_period_label"] = period_label
                period_client_rows.append(c)
            for c in r.get("_clawback_clients", []):
                c["_clawback_in_period"] = period_label
                period_client_rows.append(c)

        periods_out.append({
            "period_label": period_label,
            "filename": filename,
            "results": agent_results,
            "client_rows": period_client_rows,
            "errors": row_errors,
        })

    if not periods_out:
        periods_out.append({
            "period_label": None, "filename": filename,
            "results": [], "client_rows": [],
            "errors": row_errors + ["No commissionable rows found in file."],
        })

    return periods_out
