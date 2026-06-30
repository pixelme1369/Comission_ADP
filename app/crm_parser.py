"""
Parses the backend CRM export (one row per client) and aggregates into
per-agent, per-commission-period commission data.

A unit is CLEARED when:
  - 1st Payment Cleared Date has a value
  - Dropped Date is empty
  - Status is NOT "Pending Affiliate Cancellation"

A unit is PENDING (not yet payable) when:
  - 1st Payment Cleared Date has a value
  - Dropped Date is empty
  - Status IS "Pending Affiliate Cancellation"

A unit is CANCELLED when:
  - Dropped Date has a value

Commission period = month/year of 1st Payment Cleared Date (YYYY-MM).

NSF flag: agent is flagged if any of their clients has # NSF >= 3.

Cancellation rate = cancelled rows / (cleared + cancelled + pending) rows
for that agent in the derived period, expressed as a percentage.
"""

import csv
import io
from collections import defaultdict
from datetime import datetime

from app.calculator import calculate_agent_commission

NSF_FLAG_THRESHOLD = 3

CRM_REQUIRED_COLUMNS = {
    "sales rep",
    "1st payment cleared date",
    "dropped date",
    "status",
    "enrolled debt",
    "# nsf",
}


def _parse_date(value: str):
    """Return a datetime or None for common date formats."""
    value = value.strip()
    if not value:
        return None
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _parse_currency(value: str) -> float:
    """Strip $ and commas and return float."""
    return float(value.strip().replace("$", "").replace(",", "") or 0)


def parse_crm_and_calculate(file_bytes: bytes, filename: str) -> dict:
    """
    Parse a CRM export CSV and return commission results grouped by agent + period.

    Returns a list of period dicts, each containing:
      {
        "period_label": "2026-07",
        "filename": "...",
        "results": [ <calculator dict with extra crm fields>, ... ],
        "errors": [],
      }
    Multiple periods may be present in one CRM export file.
    """
    errors = []

    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [{"errors": ["File must be UTF-8 encoded."], "period_label": None, "filename": filename, "results": []}]

    reader = csv.DictReader(io.StringIO(text))

    if not reader.fieldnames:
        return [{"errors": ["CSV file is empty or has no header row."], "period_label": None, "filename": filename, "results": []}]

    actual_cols = {c.strip().lower() for c in reader.fieldnames if c}
    missing = CRM_REQUIRED_COLUMNS - actual_cols
    if missing:
        return [{
            "errors": [f"Missing required CRM columns: {', '.join(sorted(missing))}"],
            "period_label": None,
            "filename": filename,
            "results": [],
        }]

    # Build normalized header map: lowercase stripped → original
    col_map = {c.strip().lower(): c for c in reader.fieldnames if c}

    def get(row, key):
        return row.get(col_map.get(key, key), "").strip()

    # agent_period_key → list of client rows (as dicts)
    # key = (agent_name, period_label)
    buckets = defaultdict(list)
    row_errors = []

    for row_num, raw_row in enumerate(reader, start=2):
        agent = get(raw_row, "sales rep")
        if not agent:
            row_errors.append(f"Row {row_num}: missing Sales Rep, skipped")
            continue

        cleared_date_raw = get(raw_row, "1st payment cleared date")
        dropped_date_raw = get(raw_row, "dropped date")
        status = get(raw_row, "status")

        cleared_date = _parse_date(cleared_date_raw)
        dropped_date = _parse_date(dropped_date_raw)

        try:
            enrolled_debt = _parse_currency(get(raw_row, "enrolled debt"))
        except ValueError:
            enrolled_debt = 0.0
            row_errors.append(f"Row {row_num} ({agent}): invalid Enrolled Debt value")

        try:
            nsf_count = int(get(raw_row, "# nsf") or 0)
        except ValueError:
            nsf_count = 0

        is_pending_cancellation = status.strip().lower() == "pending affiliate cancellation"
        is_cancelled = dropped_date is not None

        # Determine which period this row contributes to
        if cleared_date and not is_cancelled:
            period_label = cleared_date.strftime("%Y-%m")
        elif cleared_date and is_cancelled:
            # Still attribute cancelled unit to the period it would have cleared in
            period_label = cleared_date.strftime("%Y-%m")
        else:
            # No cleared date — unit hasn't cleared yet, skip for commission purposes
            continue

        buckets[(agent, period_label)].append({
            "enrolled_debt": enrolled_debt,
            "is_cleared": cleared_date is not None and not is_cancelled and not is_pending_cancellation,
            "is_pending": cleared_date is not None and not is_cancelled and is_pending_cancellation,
            "is_cancelled": is_cancelled,
            "nsf_count": nsf_count,
        })

    # Aggregate per agent per period
    period_map = defaultdict(list)  # period_label → list of agent result dicts

    for (agent_name, period_label), rows in buckets.items():
        cleared_rows = [r for r in rows if r["is_cleared"]]
        pending_rows = [r for r in rows if r["is_pending"]]
        cancelled_rows = [r for r in rows if r["is_cancelled"]]

        units_cleared = len(cleared_rows)
        total_cleared_debt = sum(r["enrolled_debt"] for r in cleared_rows)
        pending_units = len(pending_rows)
        pending_debt = sum(r["enrolled_debt"] for r in pending_rows)

        total_for_rate = len(cleared_rows) + len(cancelled_rows) + len(pending_rows)
        cancellation_rate_pct = (len(cancelled_rows) / total_for_rate * 100) if total_for_rate > 0 else 0.0

        nsf_flagged = any(r["nsf_count"] >= NSF_FLAG_THRESHOLD for r in rows)

        if units_cleared == 0:
            # No cleared units to pay commission on; still record pending info
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
                "payout": 0.0,
                "payout_type": "none",
                "quality_bonus_eligible": False,
                "cancellation_penalty_applied": False,
                "nsf_flagged": nsf_flagged,
                "pending_units": pending_units,
                "pending_debt": pending_debt,
                "source": "crm",
                "notes": "No cleared units this period" + (f" | {pending_units} unit(s) pending Affiliate Cancellation review" if pending_units else ""),
            }
        else:
            result = calculate_agent_commission(
                agent_name=agent_name,
                units_cleared=units_cleared,
                total_cleared_debt=total_cleared_debt,
                cancellation_rate_pct=cancellation_rate_pct,
                hourly_draw=0.0,
            )
            result["nsf_flagged"] = nsf_flagged
            result["pending_units"] = pending_units
            result["pending_debt"] = pending_debt
            result["source"] = "crm"
            if pending_units:
                result["notes"] += f" | {pending_units} unit(s) pending Affiliate Cancellation review (${pending_debt:,.2f} debt on hold)"
            if nsf_flagged:
                result["notes"] += f" | NSF flag: one or more clients have {NSF_FLAG_THRESHOLD}+ NSF events"

        period_map[period_label].append(result)

    periods_out = []
    for period_label, results in sorted(period_map.items()):
        periods_out.append({
            "period_label": period_label,
            "filename": filename,
            "results": results,
            "errors": row_errors,
        })

    if not periods_out:
        periods_out.append({
            "period_label": None,
            "filename": filename,
            "results": [],
            "errors": row_errors + ["No commissionable rows found in file."],
        })

    return periods_out
