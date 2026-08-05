"""Tests for the prior-manager commission history import."""

import pytest

from commission_core.commission_history_parser import parse_commission_history

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


RATE_HEADER = ("Month,ID,Sales Rep,Full Name,Enrolled Debt,To subtract,Payments Made,"
               "Units,Status,Rate,Agent Month File Count\r\n")


def history_csv_with_rate(rows) -> bytes:
    return (RATE_HEADER + "".join(r + "\r\n" for r in rows)).encode("utf-8")


class TestRateColumn:
    """Owner-added "Rate" column: the exact rate a client's commission was
    actually paid at on a given row, so a later clawback can use it verbatim
    instead of recalculating a rate through the tier table. See
    known_rate_by_crm_id (crm_parser.py) for the consuming side."""

    def test_paid_rate_parsed_from_percent_string(self):
        # Matches the reported real example: AJ Valipour / Dustin Holte,
        # $42,869.00 x 1.40% = $600.17.
        parsed = parse_commission_history(history_csv_with_rate([
            "January,1181065497,AJ Valipour,Dustin Holte,42869,,2,1,Active,1.40%,32",
        ]), "hist.csv", 2026)
        assert parsed["errors"] == []
        (aj,) = by_period(parsed)["2026-01"]
        assert aj["_cleared_clients"][0]["paid_rate"] == pytest.approx(0.014)

    def test_paid_rate_parsed_without_percent_sign(self):
        parsed = parse_commission_history(history_csv_with_rate([
            "January,111,Dave,Client A,20000,,2,1,Active,1.75,",
        ]), "hist.csv", 2026)
        (dave,) = by_period(parsed)["2026-01"]
        assert dave["_cleared_clients"][0]["paid_rate"] == pytest.approx(0.0175)

    def test_missing_rate_is_none_not_an_error(self):
        parsed = parse_commission_history(history_csv_with_rate([
            "January,111,Dave,Client A,20000,,2,1,Active,,",
        ]), "hist.csv", 2026)
        assert parsed["errors"] == []
        (dave,) = by_period(parsed)["2026-01"]
        assert dave["_cleared_clients"][0]["paid_rate"] is None

    def test_rate_is_never_read_on_a_to_subtract_row(self):
        parsed = parse_commission_history(history_csv_with_rate([
            "January,111,Dave,Client A,20000,,2,1,Active,1.40%,",
            "July,111,Dave,Client A,,-250,,,Cancelled,2.25%,",
        ]), "hist.csv", 2026)
        (july,) = by_period(parsed)["2026-07"]
        assert "paid_rate" not in july["_clawback_clients"][0]
        assert july["clawback_amount"] == pytest.approx(250.0)  # unaffected, taken as-is

    def test_rate_column_optional_older_files_still_work(self):
        # No "Rate" column at all (the original HEADER, pre-feature).
        parsed = parse_commission_history(history_csv([
            "March,111,Dave,Client A,20000,,2,1,Active,",
        ]), "hist.csv", 2025)
        assert parsed["errors"] == []
        (dave,) = by_period(parsed)["2025-03"]
        assert dave["_cleared_clients"][0]["paid_rate"] is None
