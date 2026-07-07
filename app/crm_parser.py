"""
Parses a full-history CRM export (one row per client, all months in one file).

For each client row:
  - If 1st Payment Cleared Date filled + Dropped Date empty + Status != Pending Affiliate Cancellation
    → CLEARED: counts as a unit in the cleared month, commission is owed

  - If 1st Payment Cleared Date filled + Dropped Date filled + same month
    → SAME_MONTH_CANCEL: excluded from commission, NOT a clawback (never paid)

  - If 1st Payment Cleared Date filled + Dropped Date filled + different month + Payments Made
    hits the safe threshold for their Pay Freq. (Monthly=2, Biweekly=4, unknown=3) before dropping
    → SAFE_CANCEL: no clawback, regardless of whether the drop happened before or after the payout date

  - If 1st Payment Cleared Date filled + Dropped Date filled + different month + never hit the safe
    threshold + dropped before the 25th payout date
    → SAME_MONTH_CANCEL: commission was never sent, excluded, not a clawback

  - If 1st Payment Cleared Date filled + Dropped Date filled + different month + never hit the safe
    threshold + dropped on/after the payout date
    → CLAWBACK: commission was paid in the cleared month, must be deducted in the dropped month

  - If 1st Payment Cleared Date filled + Status == Pending Affiliate Cancellation
    → PENDING: not paid yet

Clawbacks are computed entirely within the parser (no DB lookups needed) since
the full history is in one file. The clawback amount is applied to the agent's
dropped-month commission period.
"""

import csv
import io
from collections import defaultdict
from datetime import datetime

from app.calculator import calculate_agent_commission, calculate_clawback_delta

NSF_FLAG_THRESHOLD = 3

# Minimum payments before clawback protection kicks in, by payment frequency
def _safe_payment_threshold(pay_freq: str) -> int:
    """Return the number of payments that protects against clawback."""
    freq = (pay_freq or "").strip().lower()
    if freq == "biweekly":
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


def parse_crm_and_calculate(file_bytes: bytes, filename: str, already_cleared_crm_ids: set = None) -> list:
    """
    Parse a full-history CRM export and return one dict per commission period found.

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

        all_clients.append({
            "crm_id": get(raw_row, "id"),
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
    # Late activation: if a client is currently cleared (active) but
    # their crm_id was never saved as is_cleared in the DB, and their
    # cleared_period is earlier than the latest period in this file,
    # they were pending before and just became active. Credit their
    # commission in the latest period instead of their cleared month.
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

    if already_cleared_crm_ids:
        all_cleared_periods = [c["cleared_period"] for c in all_clients if c["cleared_period"]]
        latest_period = max(all_cleared_periods) if all_cleared_periods else None

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
    cleared_buckets = defaultdict(list)   # (agent, period) → cleared clients
    cancel_buckets = defaultdict(list)    # (agent, period) → cancelled clients (for cancel rate)

    for c in all_clients:
        key = (c["agent_name"], c["cleared_period"])
        if c["unit_status"] == "cleared":
            cleared_buckets[key].append(c)
        elif c["unit_status"] == "clawback":
            # Only clawback clients count toward cancel rate
            # same_month_cancel and safe_cancel are excluded
            cancel_buckets[key].append(c)

    # ---------------------------------------------------------------
    # Step 2: Calculate base commission per agent per cleared period
    # Store (agent, period) → commission result dict
    # ---------------------------------------------------------------
    agent_period_results = {}  # (agent, cleared_period) → result dict

    for (agent_name, period_label), cleared in cleared_buckets.items():
        cancelled = cancel_buckets.get((agent_name, period_label), [])
        pending = [c for c in all_clients
                   if c["agent_name"] == agent_name
                   and c["cleared_period"] == period_label
                   and c["unit_status"] == "pending"]

        units_cleared = len(cleared)
        total_cleared_debt = sum(c["enrolled_debt"] for c in cleared)
        # Cancel rate = clawback clients / (cleared + clawback clients)
        # Same-month cancels, safe cancels, and pending are excluded from both sides
        total_for_rate = units_cleared + len(cancelled)
        cancel_rate_pct = (len(cancelled) / total_for_rate * 100) if total_for_rate > 0 else 0.0
        nsf_flagged = any(c["nsf_count"] >= NSF_FLAG_THRESHOLD
                          for c in cleared + cancelled + pending)

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
        result["_cleared_clients"] = cleared
        result["_all_period_clients"] = cleared + cancelled + pending

        if len(pending) > 0:
            result["notes"] += f" | {len(pending)} unit(s) pending Affiliate Cancellation review"
        if nsf_flagged:
            result["notes"] += f" | NSF flag: client(s) with {NSF_FLAG_THRESHOLD}+ NSF events"

        # Note any late activations included in this period
        late_activations = [c for c in cleared if c.get("is_late_activation")]
        if late_activations:
            periods = sorted({c["original_cleared_period"] for c in late_activations})
            result["notes"] += (
                f" | {len(late_activations)} late activation(s) — originally cleared "
                f"{', '.join(periods)}, commission credited this period"
            )

        # Commission per cleared client
        for c in cleared:
            c["commission_on_client"] = round(c["enrolled_debt"] * result["tier_rate"], 2)

        agent_period_results[(agent_name, period_label)] = result

    # ---------------------------------------------------------------
    # Step 3: Calculate clawbacks
    # For each clawback client, find their original cleared period,
    # recalculate that period's commission without them, compute delta.
    # Apply the clawback to the agent's DROPPED month period.
    # ---------------------------------------------------------------
    # (agent, dropped_period) → list of (client, clawback_amount)
    clawback_by_drop_period = defaultdict(list)

    for c in all_clients:
        if c["unit_status"] != "clawback":
            continue

        crm_id = c.get("crm_id", "")
        agent_name = c["agent_name"]
        cleared_period = c["cleared_period"]
        dropped_period = c["dropped_period"]
        orig_key = (agent_name, cleared_period)

        # Guard: only clawback if commission was actually paid on this client.
        # It was paid if the client was in the cleared bucket this file (commission
        # calculated now) OR was saved as is_cleared=True in a prior DB upload.
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
            continue

        orig_result = agent_period_results.get(orig_key)
        if not orig_result:
            # Commission record not found (agent had 0 cleared in that month after cancels)
            # Clawback = just this client's debt × lowest possible rate
            cb = round(c["enrolled_debt"] * 0.01, 2)
            c["clawback_amount"] = cb
            clawback_by_drop_period[(agent_name, dropped_period)].append(c)
            continue

        cb = calculate_clawback_delta(
            orig_units=orig_result["units_cleared"],
            orig_debt=orig_result["total_cleared_debt"],
            orig_commission=orig_result["gross_commission"],
            orig_cancellation_rate=orig_result["cancellation_rate"],
            client_debt=c["enrolled_debt"],
        )
        c["clawback_amount"] = cb
        clawback_by_drop_period[(agent_name, dropped_period)].append(c)

    # ---------------------------------------------------------------
    # Step 4: Apply clawbacks to the dropped-month period results
    # If no commission result exists for the dropped month yet,
    # create a zero-unit entry just to carry the clawback.
    # ---------------------------------------------------------------
    for (agent_name, dropped_period), cb_clients in clawback_by_drop_period.items():
        total_cb = round(sum(c["clawback_amount"] for c in cb_clients), 2)
        key = (agent_name, dropped_period)

        if key not in agent_period_results:
            # Agent had no cleared units in the dropped month — create a holding entry
            agent_period_results[key] = {
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

        r = agent_period_results[key]
        r["clawback_amount"] = round(r.get("clawback_amount", 0.0) + total_cb, 2)
        r["net_commission"] = max(0.0, round(r["gross_commission"] - r["clawback_amount"], 2))
        r["notes"] = (r.get("notes") or "") + \
            f" | Clawback -${total_cb:,.2f} from {len(cb_clients)} cancelled client(s) (prior month)"
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
