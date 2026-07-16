import csv
import io
import re

from app.calculator import calculate_agent_commission

PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")

REQUIRED_COLUMNS = {"agent_name", "units_cleared", "total_cleared_debt", "cancellation_rate", "hourly_draw", "period"}


def parse_and_calculate(file_bytes: bytes, filename: str) -> dict:
    """
    Parse CSV bytes, validate, and calculate commissions for each agent.
    Returns:
        {
            "period_label": "2026-05",
            "filename": "...",
            "results": [ <calculator dict>, ... ],
            "errors": []   # non-empty means the whole upload should be rejected
        }
    """
    errors = []
    results = []

    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return {"errors": ["File must be UTF-8 encoded."], "period_label": None, "filename": filename, "results": []}

    reader = csv.DictReader(io.StringIO(text))

    if not reader.fieldnames:
        return {"errors": ["CSV file is empty or has no header row."], "period_label": None, "filename": filename, "results": []}

    missing_cols = REQUIRED_COLUMNS - {c.strip().lower() for c in reader.fieldnames}
    if missing_cols:
        return {
            "errors": [f"Missing required columns: {', '.join(sorted(missing_cols))}"],
            "period_label": None,
            "filename": filename,
            "results": [],
        }

    period_label = None
    seen_agents = set()

    for row_num, raw_row in enumerate(reader, start=2):
        row = {k.strip().lower(): v.strip() for k, v in raw_row.items() if k}
        row_errors = []

        # period
        period = row.get("period", "").strip()
        if not period:
            row_errors.append("period is missing")
        else:
            if not PERIOD_RE.match(period):
                row_errors.append(f"period '{period}' must be YYYY-MM format")
            elif period_label is None:
                period_label = period
            elif period != period_label:
                row_errors.append(f"period '{period}' differs from earlier rows ('{period_label}'). One period per file.")

        # agent_name
        agent_name = row.get("agent_name", "").strip()
        if not agent_name:
            row_errors.append("agent_name is missing")
        elif agent_name.lower() in seen_agents:
            row_errors.append(f"duplicate agent_name '{agent_name}'")
        else:
            seen_agents.add(agent_name.lower())

        # units_cleared
        try:
            units_cleared = int(row.get("units_cleared", ""))
            if units_cleared < 1:
                row_errors.append("units_cleared must be >= 1")
        except ValueError:
            units_cleared = 0
            row_errors.append("units_cleared must be a whole number")

        # total_cleared_debt
        try:
            total_cleared_debt = float(row.get("total_cleared_debt", ""))
            if total_cleared_debt < 0:
                row_errors.append("total_cleared_debt must be >= 0")
        except ValueError:
            total_cleared_debt = 0.0
            row_errors.append("total_cleared_debt must be a number")

        # cancellation_rate
        try:
            cancellation_rate = float(row.get("cancellation_rate", ""))
            if not (0 <= cancellation_rate <= 100):
                row_errors.append("cancellation_rate must be between 0 and 100")
        except ValueError:
            cancellation_rate = 0.0
            row_errors.append("cancellation_rate must be a number")

        # hourly_draw
        try:
            hourly_draw = float(row.get("hourly_draw", ""))
            if hourly_draw < 0:
                row_errors.append("hourly_draw must be >= 0")
        except ValueError:
            hourly_draw = 0.0
            row_errors.append("hourly_draw must be a number")

        if row_errors:
            for e in row_errors:
                errors.append(f"Row {row_num} ({agent_name or 'unknown'}): {e}")
            continue

        result = calculate_agent_commission(
            agent_name=agent_name,
            units_cleared=units_cleared,
            total_cleared_debt=total_cleared_debt,
            cancellation_rate_pct=cancellation_rate,
            hourly_draw=hourly_draw,
        )
        results.append(result)

    if not results and not errors:
        errors.append("CSV has no data rows.")

    return {
        "period_label": period_label,
        "filename": filename,
        "results": results,
        "errors": errors,
    }
