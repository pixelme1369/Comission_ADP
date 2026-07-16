"""
Parses the Cordoba (funder) payout export (.xlsx). The file has 3 tabs:

- First Pays / EPF: confirm Cordoba actually paid us on a client — checked against
  our own ClientRecord IDs to flag cordoba_paid = True (purely informational).
- Chargebacks: confirms Cordoba took a marketing payout BACK from us on a client.
  Returned as raw rows here; routes.py cross-references each ID against our own
  ClientRecord history (this tab has no agent/rep column of its own) and claws back
  the agent's commission if we ever paid them on that client.
"""

import io
from datetime import date, datetime

import openpyxl

REQUIRED_SHEETS = {"first pays", "epf"}


def _clean_id(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value).strip()


def _clean_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime("%m/%d/%Y")
    return str(value).strip() or None


def _clean_currency(value) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace("$", "").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def _clean_int(value) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def _sheet_by_name(workbook, wanted_lower: str):
    for name in workbook.sheetnames:
        if name.strip().lower() == wanted_lower:
            return workbook[name]
    return None


def _header_map(sheet) -> dict:
    header_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), ())
    return {str(h).strip().lower(): idx for idx, h in enumerate(header_row) if h}


def _parse_chargebacks(workbook, errors: list) -> list:
    sheet = _sheet_by_name(workbook, "chargebacks")
    if sheet is None:
        return []

    cols = _header_map(sheet)
    if "id" not in cols:
        errors.append("Chargebacks tab is missing an 'ID' column.")
        return []
    if "dropped date" not in cols:
        errors.append("Chargebacks tab is missing a 'Dropped Date' column.")
        return []

    def cell(row, key):
        idx = cols.get(key)
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    rows = []
    for row in sheet.iter_rows(min_row=2, values_only=True):
        crm_id = _clean_id(cell(row, "id"))
        if not crm_id:
            continue
        rows.append({
            "crm_id": crm_id,
            "client_name": cell(row, "full name") or "",
            "status": cell(row, "status") or "",
            "marketing_payout_debt": _clean_currency(cell(row, "marketing payout debt")),
            "first_payment_cleared_date": _clean_date(cell(row, "1st payment cleared date")),
            "pay_freq": cell(row, "pay freq.") or "",
            "payments_made": _clean_int(cell(row, "payments made")),
            "dropped_date": _clean_date(cell(row, "dropped date")),
            "chargeback_date": _clean_date(cell(row, "marketing payment chargeback")),
        })
    return rows


def parse_cordoba_payout(file_bytes: bytes) -> dict:
    """
    Returns:
    {
        "paid_ids": [ {"crm_id": str, "client_name": str, "source": "first_pays"|"epf"}, ... ],
        "chargebacks": [ {"crm_id": str, "client_name": str, "status": str,
                           "marketing_payout_debt": float, "first_payment_cleared_date": str|None,
                           "pay_freq": str, "payments_made": int, "dropped_date": str|None,
                           "chargeback_date": str|None}, ... ],
        "epf_rows": [ {"crm_id": str, "client_name": str, "cleared_date": str|None}, ... ],
        "errors": [str, ...],
    }
    """
    errors = []

    try:
        workbook = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    except Exception:
        return {"paid_ids": [], "chargebacks": [],
                "errors": ["Could not read the file — expected an .xlsx "
                           "workbook with First Pays and EPF tabs."]}

    sheet_names_lower = {name.strip().lower() for name in workbook.sheetnames}
    missing = REQUIRED_SHEETS - sheet_names_lower
    if missing:
        errors.append(f"Missing tab(s) in Cordoba file: {', '.join(sorted(missing))}")

    paid_ids = []

    first_pays = _sheet_by_name(workbook, "first pays")
    if first_pays is not None:
        cols = _header_map(first_pays)
        if "id" not in cols:
            errors.append("First Pays tab is missing an 'ID' column.")
        else:
            for row in first_pays.iter_rows(min_row=2, values_only=True):
                crm_id = _clean_id(row[cols["id"]]) if cols["id"] < len(row) else ""
                if not crm_id:
                    continue
                client_name = row[cols["full name"]] if "full name" in cols and cols["full name"] < len(row) else ""
                paid_ids.append({"crm_id": crm_id, "client_name": client_name, "source": "first_pays"})

    epf_rows = []
    epf = _sheet_by_name(workbook, "epf")
    if epf is not None:
        cols = _header_map(epf)
        if "contact id" not in cols:
            errors.append("EPF tab is missing a 'Contact ID' column.")
        else:
            def cell(row, key):
                idx = cols.get(key)
                return row[idx] if idx is not None and idx < len(row) else None

            for row in epf.iter_rows(min_row=2, values_only=True):
                crm_id = _clean_id(cell(row, "contact id"))
                if not crm_id:
                    continue
                client_name = cell(row, "full name") or ""
                # EPF entries still confirm Cordoba paid us on the client (CordobaPaidClient
                # ledger / chargeback gate 2) ...
                paid_ids.append({"crm_id": crm_id, "client_name": client_name, "source": "epf"})
                # ... and additionally feed the display-only EPF section, placed in the
                # month of the tab's Cleared Date.
                epf_rows.append({
                    "crm_id": crm_id,
                    "client_name": client_name,
                    "cleared_date": _clean_date(cell(row, "cleared date")),
                })

    chargebacks = _parse_chargebacks(workbook, errors)

    return {"paid_ids": paid_ids, "chargebacks": chargebacks, "epf_rows": epf_rows, "errors": errors}
