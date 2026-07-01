"""
Parses the backend CRM export (one row per client) and aggregates into
per-agent, per-commission-period commission data.

Cleared unit rules:
  - 1st Payment Cleared Date has a value
  - Dropped Date is empty
  - Status != "Pending Affiliate Cancellation"

Same-month cancel: Cleared Date and Dropped Date in the same month → excluded,
never paid, NOT a clawback.

Clawback (handled in routes.py after DB lookup):
  - Dropped Date in a LATER month than 1st Payment Cleared Date
  - Payments Made < 3
"""

import csv
import io
from collections import defaultdict
from datetime import datetime

from app.calculator import calculate_agent_commission, get_tier, TIERS

NSF_FLAG_THRESHOLD = 3

CRM_REQUIRED_COLUMNS = {
    "sales rep",
    "1st payment cleared date",
    "dropped date",
    "status",
    "enrolled debt",
    "# nsf",
}

OPTIONAL_COLUMNS = [
    "id", "full name", "email", "home phone", "stage",
    "submitted date", "enrolled date",
    "1st payment date", "2nd payment cleared date", "payments made",
]


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


def _parse_currency(value: str) -> float:
    return float(value.strip().replace("$", "").replace(",", "") or 0)


def parse_crm_and_calculate(file_bytes: bytes, filename: str) -> list:
    """
    Parse CRM export. Returns a list of period dicts:
    {
        "period_label": "2026-06",
        "filename": str,
        "results": [ agent_result_dict, ... ],
        "client_rows": [ client_row_dict, ... ],   # all individual clients for this period
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

    # (agent_name, period_label) → list of parsed client dicts
    buckets = defaultdict(list)
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

        is_pending_cancellation = status.strip().lower() == "pending affiliate cancellation"
        is_cancelled = dropped_date is not None
        cleared_period = _period_of(cleared_date)
        dropped_period = _period_of(dropped_date)

        # Same-month cancel: cleared and dropped in same month → exclude entirely
        same_month_cancel = (cleared_date and dropped_date and cleared_period == dropped_period)

        if cleared_date and not is_cancelled and not is_pending_cancellation:
            unit_status = "cleared"
        elif cleared_date and not is_cancelled and is_pending_cancellation:
            unit_status = "pending"
        elif same_month_cancel:
            unit_status = "same_month_cancel"  # excluded from commission AND not a clawback
        elif is_cancelled and cleared_date and cleared_period != dropped_period:
            unit_status = "clawback_candidate"  # paid in cleared_period, cancelled in dropped_period
        elif is_cancelled and not cleared_date:
            unit_status = "cancelled_never_cleared"  # never paid, ignore
        else:
            unit_status = "not_yet_cleared"

        # Only include rows that have a cleared date (or are clawback candidates)
        if not cleared_date and unit_status not in ("same_month_cancel",):
            continue

        period_label = cleared_period  # attribute to the period when it cleared

        client_dict = {
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
            "payments_made": payments_made,
            "nsf_count": nsf_count,
            "enrolled_debt": enrolled_debt,
            "unit_status": unit_status,
            "cleared_period": cleared_period,
            "dropped_period": dropped_period,
            "is_cleared": unit_status == "cleared",
            "is_pending": unit_status == "pending",
            "is_cancelled": is_cancelled,
        }

        if period_label:
            buckets[(agent, period_label)].append(client_dict)

    # Aggregate per agent per period
    period_map = defaultdict(lambda: {"results": [], "client_rows": []})

    for (agent_name, period_label), rows in buckets.items():
        cleared_rows = [r for r in rows if r["unit_status"] == "cleared"]
        pending_rows = [r for r in rows if r["unit_status"] == "pending"]
        cancelled_rows = [r for r in rows if r["unit_status"] in ("same_month_cancel", "clawback_candidate")]

        units_cleared = len(cleared_rows)
        total_cleared_debt = sum(r["enrolled_debt"] for r in cleared_rows)
        pending_units = len(pending_rows)
        pending_debt = sum(r["enrolled_debt"] for r in pending_rows)

        total_for_rate = len(cleared_rows) + len(cancelled_rows) + len(pending_rows)
        cancellation_rate_pct = (len(cancelled_rows) / total_for_rate * 100) if total_for_rate > 0 else 0.0

        nsf_flagged = any(r["nsf_count"] >= NSF_FLAG_THRESHOLD for r in rows)

        if units_cleared == 0:
            result = {
                "agent_name": agent_name,
                "units_cleared": 0,
                "total_cleared_debt": 0.0,
                "cancellation_rate": round(cancellation_rate_pct, 2),
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
                "nsf_flagged": nsf_flagged,
                "pending_units": pending_units,
                "pending_debt": pending_debt,
                "source": "crm",
                "notes": "No cleared units this period" + (
                    f" | {pending_units} unit(s) pending Affiliate Cancellation review" if pending_units else ""),
            }
            tier_rate = 0.0
        else:
            result = calculate_agent_commission(
                agent_name=agent_name,
                units_cleared=units_cleared,
                total_cleared_debt=total_cleared_debt,
                cancellation_rate_pct=cancellation_rate_pct,
                hourly_draw=0.0,
            )
            result["clawback_amount"] = 0.0
            result["net_commission"] = result["gross_commission"]
            result["nsf_flagged"] = nsf_flagged
            result["pending_units"] = pending_units
            result["pending_debt"] = pending_debt
            result["source"] = "crm"
            tier_rate = result["tier_rate"]
            if pending_units:
                result["notes"] += f" | {pending_units} unit(s) pending Affiliate Cancellation review (${pending_debt:,.2f} on hold)"
            if nsf_flagged:
                result["notes"] += f" | NSF flag: one or more clients have {NSF_FLAG_THRESHOLD}+ NSF events"

        # Annotate each client with their individual commission contribution
        for r in cleared_rows:
            r["commission_on_client"] = round(r["enrolled_debt"] * tier_rate, 2)
        for r in pending_rows + cancelled_rows:
            r["commission_on_client"] = 0.0

        result["_client_rows"] = rows  # carry clients along for DB insertion
        period_map[period_label]["results"].append(result)
        period_map[period_label]["client_rows"].extend(rows)

    periods_out = []
    for period_label, data in sorted(period_map.items()):
        periods_out.append({
            "period_label": period_label,
            "filename": filename,
            "results": data["results"],
            "client_rows": data["client_rows"],
            "errors": row_errors,
        })

    if not periods_out:
        periods_out.append({
            "period_label": None, "filename": filename,
            "results": [], "client_rows": [],
            "errors": row_errors + ["No commissionable rows found in file."],
        })

    return periods_out
