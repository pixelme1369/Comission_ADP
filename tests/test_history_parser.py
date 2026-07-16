"""Tests for the prior-manager commission history import."""

import pytest

from app.commission_history_parser import parse_commission_history

HEADER = "Month,ID,Sales Rep,Full Name,Enrolled Debt,To subtract,Payments Made,Units,Status,Marketing Campaign\r\n"


def history_csv(rows) -> bytes:
    return (HEADER + "".join(r + "\r\n" for r in rows)).encode("utf-8")


def by_period(parsed):
    return {p["period_label"]: p["results"] for p in parsed["periods"]}


def test_paid_rows_run_through_tier_math():
    parsed = parse_commission_history(history_csv([
        "March,111,Dave,Client A,20000,,2,1,Active,Campaign X",
        "March,222,Dave,Client B,30000,,2,1,Active,Campaign X",
    ]), "hist.csv", 2025)
    assert parsed["errors"] == []
    (dave,) = by_period(parsed)["2025-03"]
    assert dave["units_cleared"] == 2
    assert dave["gross_commission"] == pytest.approx(500.0)  # 50,000 x 1% Tier 1


def test_to_subtract_amount_used_as_is():
    parsed = parse_commission_history(history_csv([
        "March,111,Dave,Client A,20000,,2,1,Active,",
        "April,111,Dave,Client A,,-250,,,Cancelled,",
    ]), "hist.csv", 2025)
    periods = by_period(parsed)
    (april,) = periods["2025-04"]
    assert april["units_cleared"] == 0
    assert april["clawback_amount"] == pytest.approx(250.0)  # taken as-is, not recomputed
    assert april["net_commission"] == 0.0


def test_bad_payments_made_does_not_crash():
    parsed = parse_commission_history(history_csv([
        "March,111,Dave,Client A,20000,,N/A,1,Active,",
    ]), "hist.csv", 2025)
    assert parsed["errors"] == []
    (dave,) = by_period(parsed)["2025-03"]
    assert dave["_cleared_clients"][0]["payments_made"] == 0


def test_row_with_neither_amount_is_reported():
    parsed = parse_commission_history(history_csv([
        "March,111,Dave,Client A,,,2,1,Active,",
    ]), "hist.csv", 2025)
    assert any("neither Enrolled Debt nor" in e for e in parsed["errors"])


def test_missing_columns_rejected():
    parsed = parse_commission_history(b"Month,ID\r\nMarch,1\r\n", "hist.csv", 2025)
    assert parsed["periods"] == []
    assert "Missing column(s)" in parsed["errors"][0]


def test_excel_float_ids_are_normalized():
    parsed = parse_commission_history(history_csv([
        "March,1181065497.0,Dave,Client A,20000,,2,1,Active,",
    ]), "hist.csv", 2025)
    (dave,) = by_period(parsed)["2025-03"]
    assert dave["_cleared_clients"][0]["crm_id"] == "1181065497"
