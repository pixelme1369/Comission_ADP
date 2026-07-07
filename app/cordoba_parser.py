"""
Parses the Cordoba (funder) payout export (.xlsx). The file has 3 tabs (First Pays,
EPF, Chargebacks) but this app only checks client IDs against First Pays and EPF —
those are the tabs that confirm Cordoba actually paid us on a file. Chargebacks is
intentionally ignored here.
"""

import io

import openpyxl

REQUIRED_SHEETS = {"first pays", "epf"}


def _clean_id(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value).strip()


def _sheet_by_name(workbook, wanted_lower: str):
    for name in workbook.sheetnames:
        if name.strip().lower() == wanted_lower:
            return workbook[name]
    return None


def _header_map(sheet) -> dict:
    header_row = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), ())
    return {str(h).strip().lower(): idx for idx, h in enumerate(header_row) if h}


def parse_cordoba_payout(file_bytes: bytes) -> dict:
    """
    Returns:
    {
        "paid_ids": [ {"crm_id": str, "client_name": str, "source": "first_pays"|"epf"}, ... ],
        "errors": [str, ...],
    }
    """
    errors = []

    try:
        workbook = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    except Exception:
        return {"paid_ids": [], "errors": ["Could not read the file — expected an .xlsx "
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

    return {"paid_ids": paid_ids, "errors": errors}
