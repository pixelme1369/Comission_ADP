"""
Parses the weekly Cordoba (funder) payout export (.xlsx) with three tabs:

  - First Pays: files Cordoba paid us on this week (front-end commission)
  - EPF: files Cordoba paid us on this week (rev-share)
  - Chargebacks: files Cordoba clawed back from us this week

This module only reads and normalizes the file. Matching against our own
ClientRecord/AgentCommission data and all DB writes happen in routes.py.
"""

import io
from datetime import datetime

import openpyxl

REQUIRED_SHEETS = {"first pays", "epf", "chargebacks"}


def _clean_id(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value).strip()


def _parse_cell_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _period_of(dt) -> str:
    return dt.strftime("%Y-%m") if dt else None


def _date_str(dt) -> str:
    return dt.strftime("%Y-%m-%d") if dt else ""


def _sheet_by_name(workbook, wanted_lower: str):
    for name in workbook.sheetnames:
        if name.strip().lower() == wanted_lower:
            return workbook[name]
    return None


def _header_map(sheet) -> dict:
    header_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), ())
    return {str(h).strip().lower(): idx for idx, h in enumerate(header_row) if h}


def _parse_currency(value) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace("$", "").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def parse_cordoba_payout(file_bytes: bytes) -> dict:
    """
    Returns:
    {
        "paid_ids": [ {"crm_id": str, "client_name": str, "source": "first_pays"|"epf"}, ... ],
        "chargebacks": [ {
            "crm_id": str, "client_name": str, "marketing_payout_debt": float,
            "orig_period": "YYYY-MM"|None, "target_period": "YYYY-MM"|None,
            "chargeback_date": "YYYY-MM-DD"|"", "dropped_date": "YYYY-MM-DD"|"",
        }, ... ],
        "errors": [str, ...],
    }
    """
    errors = []

    try:
        workbook = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    except Exception:
        return {"paid_ids": [], "chargebacks": [],
                "errors": ["Could not read the file — expected an .xlsx workbook with "
                           "First Pays, EPF, and Chargebacks tabs."]}

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

    epf = _sheet_by_name(workbook, "epf")
    if epf is not None:
        cols = _header_map(epf)
        if "contact id" not in cols:
            errors.append("EPF tab is missing a 'Contact ID' column.")
        else:
            for row in epf.iter_rows(min_row=2, values_only=True):
                crm_id = _clean_id(row[cols["contact id"]]) if cols["contact id"] < len(row) else ""
                if not crm_id:
                    continue
                client_name = row[cols["full name"]] if "full name" in cols and cols["full name"] < len(row) else ""
                paid_ids.append({"crm_id": crm_id, "client_name": client_name, "source": "epf"})

    chargebacks = []
    chargebacks_sheet = _sheet_by_name(workbook, "chargebacks")
    if chargebacks_sheet is not None:
        cols = _header_map(chargebacks_sheet)
        required = {"id", "marketing payout debt", "1st payment cleared date", "marketing payment chargeback"}
        missing_cols = required - set(cols.keys())
        if missing_cols:
            errors.append(f"Chargebacks tab is missing column(s): {', '.join(sorted(missing_cols))}")
        else:
            for row_num, row in enumerate(chargebacks_sheet.iter_rows(min_row=2, values_only=True), start=2):
                crm_id = _clean_id(row[cols["id"]]) if cols["id"] < len(row) else ""
                if not crm_id:
                    continue

                client_name = row[cols["full name"]] if "full name" in cols and cols["full name"] < len(row) else ""
                debt = _parse_currency(row[cols["marketing payout debt"]])
                cleared_date = _parse_cell_date(row[cols["1st payment cleared date"]])
                chargeback_date = _parse_cell_date(row[cols["marketing payment chargeback"]])
                dropped_date = _parse_cell_date(row[cols["dropped date"]]) if "dropped date" in cols and cols["dropped date"] < len(row) else None

                if not chargeback_date:
                    errors.append(f"Chargebacks row {row_num} (ID {crm_id}): missing Marketing Payment "
                                   "Chargeback date, skipped.")
                    continue

                chargebacks.append({
                    "crm_id": crm_id,
                    "client_name": client_name,
                    "marketing_payout_debt": debt,
                    "orig_period": _period_of(cleared_date),
                    "target_period": _period_of(chargeback_date),
                    "chargeback_date": _date_str(chargeback_date),
                    "dropped_date": _date_str(dropped_date),
                })

    return {"paid_ids": paid_ids, "chargebacks": chargebacks, "errors": errors}
