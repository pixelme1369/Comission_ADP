"""Locks in the CRM classification and clawback rules, including the two decisions
confirmed by the owner in July 2026:
  - an enrolled-then-cancelled client counts toward the cancellation rate even if
    the agent was never paid on them (pending -> cancelled);
  - a client is never clawed back twice (see also test_cordoba_chargebacks.py).
"""

import csv
import io

import pytest

from app.crm_parser import parse_crm_and_calculate, _safe_payment_threshold

HEADERS = [
    "ID", "Sales Rep", "Full Name", "1st Payment Cleared Date", "Dropped Date",
    "Status", "Enrolled Debt", "# NSF", "Payments Made", "Pay Freq.",
]


def crm_csv(rows) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=HEADERS)
    writer.writeheader()
    for r in rows:
        writer.writerow({h: r.get(h, "") for h in HEADERS})
    return out.getvalue().encode("utf-8")


def client(crm_id, cleared="", dropped="", status="Active", debt="10000",
           payments="0", freq="Monthly", rep="Maria", name="Client", nsf="0"):
    return {
        "ID": crm_id, "Sales Rep": rep, "Full Name": name,
        "1st Payment Cleared Date": cleared, "Dropped Date": dropped,
        "Status": status, "Enrolled Debt": debt, "# NSF": nsf,
        "Payments Made": payments, "Pay Freq.": freq,
    }


def by_period(periods):
    return {p["period_label"]: p for p in periods if p["period_label"]}


class TestSafeThreshold:
    def test_thresholds(self):
        assert _safe_payment_threshold("Monthly") == 2
        assert _safe_payment_threshold("biweekly") == 4
        assert _safe_payment_threshold("") == 3
        assert _safe_payment_threshold(None) == 3
        assert _safe_payment_threshold("weird") == 3


class TestClassification:
    def test_cleared_clients_grouped_by_month(self):
        data = crm_csv([
            client("A1", cleared="06/10/2026", debt="20000"),
            client("A2", cleared="06/12/2026", debt="30000"),
        ])
        periods = by_period(parse_crm_and_calculate(data, "f.csv"))
        assert list(periods) == ["2026-06"]
        (result,) = periods["2026-06"]["results"]
        assert result["units_cleared"] == 2
        assert result["gross_commission"] == pytest.approx(500.0)  # 50,000 x 1% (Tier 1)

    def test_same_month_cancel_excluded_no_clawback(self):
        data = crm_csv([
            client("A1", cleared="06/10/2026"),
            client("A2", cleared="06/05/2026", dropped="06/20/2026"),
        ])
        periods = by_period(parse_crm_and_calculate(data, "f.csv"))
        (result,) = periods["2026-06"]["results"]
        assert result["units_cleared"] == 1
        assert result["clawback_amount"] == 0.0
        assert result["cancellation_rate"] == 0.0

    def test_dropped_before_payout_date_is_not_a_clawback(self):
        # Cleared June -> payout July 25. Dropped July 10, below threshold:
        # commission was never sent, so exclude, don't claw back.
        data = crm_csv([
            client("A1", cleared="06/10/2026"),
            client("A2", cleared="06/12/2026", dropped="07/10/2026", payments="1"),
        ])
        periods = by_period(parse_crm_and_calculate(
            data, "f.csv", already_cleared_crm_ids={"A1", "A2"}))
        assert "2026-07" not in periods
        (result,) = periods["2026-06"]["results"]
        assert result["clawback_amount"] == 0.0

    def test_safe_cancel_no_clawback_even_after_payout_date(self):
        # Monthly threshold = 2 payments; client made 2 before dropping in August.
        data = crm_csv([
            client("A1", cleared="06/10/2026"),
            client("A2", cleared="06/12/2026", dropped="08/03/2026", payments="2"),
        ])
        periods = by_period(parse_crm_and_calculate(
            data, "f.csv", already_cleared_crm_ids={"A1", "A2"}))
        assert "2026-08" not in periods
        (result,) = periods["2026-06"]["results"]
        assert result["clawback_amount"] == 0.0
        assert result["cancellation_rate"] == 0.0  # safe cancels don't count in the rate

    def test_biweekly_needs_four_payments_to_be_safe(self):
        data = crm_csv([
            client("A1", cleared="06/10/2026"),
            client("A2", cleared="06/12/2026", dropped="08/03/2026",
                   payments="3", freq="Biweekly"),
        ])
        periods = by_period(parse_crm_and_calculate(
            data, "f.csv", already_cleared_crm_ids={"A1", "A2"}))
        # 3 < 4 -> not safe -> clawback lands in August
        assert periods["2026-08"]["results"][0]["clawback_amount"] > 0

    def test_pending_held_until_threshold(self):
        data = crm_csv([
            client("A1", cleared="06/10/2026"),
            client("A2", cleared="06/12/2026",
                   status="Pending Affiliate Cancellation", payments="1"),
        ])
        periods = by_period(parse_crm_and_calculate(data, "f.csv"))
        (result,) = periods["2026-06"]["results"]
        assert result["units_cleared"] == 1
        assert result["pending_units"] == 1

    def test_pending_at_threshold_counts_as_cleared(self):
        data = crm_csv([
            client("A1", cleared="06/12/2026",
                   status="Pending Affiliate Cancellation", payments="2"),
        ])
        periods = by_period(parse_crm_and_calculate(data, "f.csv"))
        (result,) = periods["2026-06"]["results"]
        assert result["units_cleared"] == 1
        assert result["pending_units"] == 0


class TestClawback:
    def test_clawback_deducted_in_dropped_month(self):
        # A2 was paid on the June period (prior upload), dropped Aug 3 with 1 payment.
        data = crm_csv([
            client("A1", cleared="06/10/2026", debt="20000"),
            client("A3", cleared="06/11/2026", debt="20000"),
            client("A2", cleared="06/12/2026", dropped="08/03/2026",
                   payments="1", debt="10000"),
        ])
        periods = by_period(parse_crm_and_calculate(
            data, "f.csv", already_cleared_crm_ids={"A1", "A2", "A3"}))

        aug = periods["2026-08"]["results"][0]
        assert aug["units_cleared"] == 0                       # holding entry
        # June recomputed: 2 units Tier 1, cancel rate 1/3 = 33% -> penalty (still Tier 1).
        # No tier change on removal -> clawback = client share = 10,000 x 1%.
        assert aug["clawback_amount"] == pytest.approx(100.0)
        assert aug["net_commission"] == 0.0

    def test_first_upload_without_db_history_never_claws_back(self):
        # Fresh DB: the app never recorded paying this client, so no clawback.
        data = crm_csv([
            client("A1", cleared="06/10/2026"),
            client("A2", cleared="06/12/2026", dropped="08/03/2026", payments="1"),
        ])
        periods = by_period(parse_crm_and_calculate(data, "f.csv"))
        assert "2026-08" not in periods

    def test_already_charged_back_via_cordoba_is_skipped(self):
        # The other half of the never-claw-back-twice rule: Cordoba got there first.
        data = crm_csv([
            client("A1", cleared="06/10/2026"),
            client("A2", cleared="06/12/2026", dropped="08/03/2026", payments="1"),
        ])
        periods = by_period(parse_crm_and_calculate(
            data, "f.csv",
            already_cleared_crm_ids={"A1", "A2"},
            already_charged_back_crm_ids={"A2"},
        ))
        assert "2026-08" not in periods


class TestCancellationRatePolicy:
    def test_never_paid_cancels_still_count_in_the_rate(self):
        """OWNER POLICY (July 2026): an enrolled client who cancelled counts toward
        the cancellation rate even if commission was never paid on them.
        3 cleared + 1 never-paid cancel -> 25% > 20% -> tier penalty applies,
        but NO clawback is charged (they were never paid)."""
        data = crm_csv([
            client("A1", cleared="06/10/2026", debt="100000"),
            client("A2", cleared="06/11/2026", debt="100000"),
            client("A3", cleared="06/12/2026", debt="100000"),
            # Cleared June, dropped after the July 25 payout date, below threshold,
            # never recorded as paid (fresh DB) -> reclassified, never paid.
            client("A4", cleared="06/13/2026", dropped="08/03/2026", payments="1"),
        ])
        periods = by_period(parse_crm_and_calculate(data, "f.csv"))
        (june,) = periods["2026-06"]["results"]
        assert june["cancellation_rate"] == pytest.approx(25.0)
        assert june["cancellation_penalty_applied"] is True
        assert "2026-08" not in periods  # but no clawback was charged


class TestLateActivation:
    def test_late_activation_credits_latest_period(self):
        data = crm_csv([
            client("A1", cleared="05/10/2026"),   # never in DB -> late activation
            client("B1", cleared="06/10/2026"),   # known from prior upload
        ])
        periods = by_period(parse_crm_and_calculate(
            data, "f.csv", already_cleared_crm_ids={"B1"}))
        assert "2026-05" not in periods
        (june,) = periods["2026-06"]["results"]
        assert june["units_cleared"] == 2

    def test_late_activation_skipped_on_fresh_db(self):
        """The empty-DB guard: a first-ever multi-month upload must keep each
        client in their own cleared month (this regressed once — see CLAUDE.md)."""
        data = crm_csv([
            client("A1", cleared="05/10/2026"),
            client("B1", cleared="06/10/2026"),
        ])
        periods = by_period(parse_crm_and_calculate(data, "f.csv"))
        assert set(periods) == {"2026-05", "2026-06"}
        assert periods["2026-05"]["results"][0]["units_cleared"] == 1
        assert periods["2026-06"]["results"][0]["units_cleared"] == 1


class TestValidation:
    def test_missing_required_columns_rejected(self):
        out = parse_crm_and_calculate(b"Sales Rep,Status\r\nMaria,Active\r\n", "f.csv")
        assert out[0]["errors"]
        assert "Missing required CRM columns" in out[0]["errors"][0]

    def test_row_without_sales_rep_skipped_with_warning(self):
        data = crm_csv([
            client("A1", cleared="06/10/2026"),
            client("A2", cleared="06/11/2026", rep=""),
        ])
        periods = parse_crm_and_calculate(data, "f.csv")
        assert any("missing Sales Rep" in e for e in periods[0]["errors"])
