"""Locks in the CRM classification and clawback rules, including the two decisions
confirmed by the owner in July 2026:
  - an enrolled-then-cancelled client counts toward the cancellation rate even if
    the agent was never paid on them (pending -> cancelled);
  - a client is never clawed back twice (see also test_cordoba_chargebacks.py).
"""

import csv
import io

import pytest

from agent_portal.crm_parser import parse_crm_and_calculate, _safe_payment_threshold

HEADERS = [
    "ID", "Sales Rep", "Full Name", "1st Payment Cleared Date", "Dropped Date",
    "Status", "Enrolled Debt", "# NSF", "Payments Made", "Pay Freq.", "Credit Score",
]


def crm_csv(rows) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=HEADERS)
    writer.writeheader()
    for r in rows:
        writer.writerow({h: r.get(h, "") for h in HEADERS})
    return out.getvalue().encode("utf-8")


def client(crm_id, cleared="", dropped="", status="Active", debt="10000",
           payments="0", freq="Monthly", rep="Maria", name="Client", nsf="0",
           credit_score=""):
    return {
        "ID": crm_id, "Sales Rep": rep, "Full Name": name,
        "1st Payment Cleared Date": cleared, "Dropped Date": dropped,
        "Status": status, "Enrolled Debt": debt, "# NSF": nsf,
        "Payments Made": payments, "Pay Freq.": freq, "Credit Score": credit_score,
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

    def test_hyphenated_biweekly_matches_the_real_crm_spelling(self):
        # The actual CRM export spells this "Bi-Weekly" (with a hyphen) — the
        # comparison must not silently fall through to the unknown-freq fallback.
        assert _safe_payment_threshold("Bi-Weekly") == 4
        assert _safe_payment_threshold("bi-weekly") == 4

    def test_semimonthly_is_treated_same_as_biweekly(self):
        # Owner confirmed (July 2026): Semi-Monthly pays out on the same cadence
        # as Bi-Weekly for clawback-safety purposes, so it needs the same 4-payment
        # threshold rather than falling into the generic unknown-freq fallback.
        assert _safe_payment_threshold("Semi-Monthly") == 4
        assert _safe_payment_threshold("semimonthly") == 4
        assert _safe_payment_threshold("Semi Monthly") == 4


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

    def test_same_month_cancel_client_is_still_saved_for_display(self):
        """agent_portal-only divergence: same-month cancels don't affect any
        money math (asserted above), but they ARE surfaced in client_rows so
        agents can see who dropped, unlike the internal app which discards
        them entirely."""
        data = crm_csv([
            client("A1", cleared="06/10/2026"),
            client("A2", cleared="06/05/2026", dropped="06/20/2026", name="Dropped Client"),
        ])
        periods = by_period(parse_crm_and_calculate(data, "f.csv"))
        rows = periods["2026-06"]["client_rows"]
        dropped_row = next(c for c in rows if c["crm_id"] == "A2")
        assert dropped_row["client_name"] == "Dropped Client"
        assert dropped_row["is_cleared"] is False
        assert dropped_row["is_pending"] is False
        assert dropped_row["is_cancelled"] is True
        assert dropped_row["commission_on_client"] == 0.0
        # still didn't move the needle on the agent's numbers
        (result,) = periods["2026-06"]["results"]
        assert result["units_cleared"] == 1
        assert result["gross_commission"] == pytest.approx(100.0)  # 10,000 x 1%, A1 only

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

    def test_safe_cancel_counts_as_zero_dollar_unit(self):
        # OWNER POLICY (confirmed July 2026): a safe-cancel client still counts as a
        # full unit toward the agent's tier (they protected the commission by hitting
        # the payment threshold before dropping) but earns $0 commission themselves —
        # same "unit credited, no dollars" treatment as a Credit Score <= 500 client.
        data = crm_csv([
            client("A1", cleared="06/10/2026", debt="10000"),
            client("A2", cleared="06/12/2026", dropped="08/03/2026",
                   payments="2", debt="10000"),
        ])
        periods = by_period(parse_crm_and_calculate(
            data, "f.csv", already_cleared_crm_ids={"A1", "A2"}))
        (result,) = periods["2026-06"]["results"]
        assert result["units_cleared"] == 2  # A1 cleared + A2 safe_cancel
        assert result["total_cleared_debt"] == pytest.approx(10000.0)  # A2's debt excluded
        assert result["gross_commission"] == pytest.approx(100.0)  # only A1's debt x 1%
        assert "safe cancel" in result["notes"]

        clients_by_id = {c["crm_id"]: c for c in result["_all_period_clients"]}
        assert clients_by_id["A2"]["unit_status"] == "safe_cancel"
        assert clients_by_id["A2"]["commission_on_client"] == 0.0
        assert clients_by_id["A2"]["is_cleared"] is False  # never eligible for a Cordoba clawback

    def test_safe_cancel_only_period_still_gets_a_result(self):
        # An agent/period with ONLY a safe-cancel unit (no plain "cleared" client at
        # all) must still produce a commission result carrying that unit — not be
        # silently dropped because cleared_buckets has no entry for the key.
        data = crm_csv([
            client("A1", cleared="06/12/2026", dropped="08/03/2026", payments="2"),
        ])
        periods = by_period(parse_crm_and_calculate(
            data, "f.csv", already_cleared_crm_ids={"A1"}))
        (result,) = periods["2026-06"]["results"]
        assert result["units_cleared"] == 1
        assert result["total_cleared_debt"] == 0.0
        assert result["gross_commission"] == 0.0

    def test_biweekly_needs_four_payments_to_be_safe(self):
        data = crm_csv([
            client("A1", cleared="06/10/2026"),
            client("A2", cleared="06/12/2026", dropped="08/03/2026",
                   payments="3", freq="Biweekly"),
        ])
        periods = by_period(parse_crm_and_calculate(
            data, "f.csv", already_cleared_crm_ids={"A1", "A2"}))
        # 3 < 4 -> not safe -> clawback applies, but lands in the latest period
        # in the file (June, the only cleared month here) rather than the
        # August drop month (owner policy, July 2026 — see TestClawback).
        assert "2026-08" not in periods
        assert periods["2026-06"]["results"][0]["clawback_amount"] > 0

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
    def test_clawback_deducted_in_latest_period_in_file(self):
        """Owner policy (confirmed July 2026): a clawback is booked against the
        LATEST period found anywhere in the file, not the client's own dropped
        month. Rationale: the file represents "as of now," and its latest month
        is effectively the payment run about to go out — an already-paid client
        caught dropping should reduce THAT payout, not a separate calendar month
        that may already be in the past. This file only contains June-cleared
        clients, so even though A2 drops in August, the deduction lands on
        June's own commission instead of creating a separate August entry."""
        data = crm_csv([
            client("A1", cleared="06/10/2026", debt="20000"),
            client("A3", cleared="06/11/2026", debt="20000"),
            client("A2", cleared="06/12/2026", dropped="08/03/2026",
                   payments="1", debt="10000"),
        ])
        periods = by_period(parse_crm_and_calculate(
            data, "f.csv", already_cleared_crm_ids={"A1", "A2", "A3"}))

        assert "2026-08" not in periods
        june = periods["2026-06"]["results"][0]
        assert june["units_cleared"] == 2                        # A1, A3 only
        assert june["gross_commission"] == pytest.approx(400.0)  # 40,000 x 1%
        # cancel rate 1/3 = 33% -> penalty, but already Tier 1 (floor) -> no change.
        # clawback = client share = 10,000 x 1%.
        assert june["clawback_amount"] == pytest.approx(100.0)
        assert june["net_commission"] == pytest.approx(300.0)

    def test_clawback_from_earlier_cleared_month_lands_in_latest_period(self):
        """The concrete real-world scenario this policy was built for: a client
        cleared in March (already paid, per already_cleared_crm_ids), and drops
        in June — after their own March payout date (April 25) but the file's
        LATEST cleared month is May (from B1). The deduction lands on May, not
        June or March, since May is effectively "the payment run about to go
        out" (paid 6/25) as of this file — owner-confirmed, July 2026."""
        data = crm_csv([
            client("B1", cleared="05/10/2026", debt="20000"),
            client("OLD1", cleared="03/05/2026", dropped="06/23/2026",
                   payments="1", debt="10000"),
        ])
        periods = by_period(parse_crm_and_calculate(
            data, "f.csv", already_cleared_crm_ids={"B1", "OLD1"}))

        assert "2026-06" not in periods
        assert "2026-03" not in periods
        may = periods["2026-05"]["results"][0]
        assert may["units_cleared"] == 1                        # B1 only
        assert may["gross_commission"] == pytest.approx(200.0)  # 20,000 x 1%
        assert may["clawback_amount"] == pytest.approx(100.0)   # fallback: 10,000 x 1%
        assert may["net_commission"] == pytest.approx(100.0)

    def test_clawback_applies_even_on_the_very_first_upload(self):
        """OWNER POLICY (confirmed August 2026, supersedes the prior "no proof
        of payment = no clawback" rule tested here before): a real 1st
        Payment Cleared Date on the row IS proof of payment — the agent was
        paid for that client in the real world regardless of whether this
        portal's own upload history has a prior record of it. A clawback now
        applies on the very first time a client is ever seen, same as any
        other upload. (The safe-payment-threshold, same-month-cancel, and
        low-credit rules are unaffected — only the "we've never recorded
        paying them before" guard was removed.)"""
        data = crm_csv([
            client("A1", cleared="06/10/2026"),
            client("A2", cleared="06/12/2026", dropped="08/03/2026", payments="1"),
        ])
        periods = by_period(parse_crm_and_calculate(data, "f.csv"))
        # Latest cleared month in this file is June, so — per the separate,
        # unrelated "latest period in file" policy — that's still where the
        # deduction lands, not August.
        assert "2026-08" not in periods
        june = periods["2026-06"]["results"][0]
        assert june["clawback_amount"] == pytest.approx(100.0)  # 10,000 x 1%
        assert june["net_commission"] == pytest.approx(0.0)     # 100 gross - 100 clawback

    def test_solo_first_time_clawback_client_still_shows_up(self):
        """The exact real-world row reported: cleared 04/10, dropped a later
        month (07/23), 1 payment, no Pay Freq. on the row (falls back to the
        3-payment threshold), and this is the very first time the portal has
        ever seen this client — previously reclassified away with no
        deduction (see test_clawback_applies_even_on_the_very_first_upload
        above for that rule's removal); now a real clawback. Also exercises
        the Step 4 "no cleared units in the target period" holding-entry path
        end to end, since this client is Josh's ONLY activity in the file."""
        data = crm_csv([
            client("SOLO1", cleared="04/10/2026", dropped="07/23/2026",
                   payments="1", freq="", debt="16866", name="Shelleen Roseborough",
                   rep="Josh Hallwork"),
        ])
        periods = by_period(parse_crm_and_calculate(data, "f.csv"))

        assert "2026-04" in periods
        (april,) = periods["2026-04"]["results"]
        assert april["units_cleared"] == 0  # a clawback never counts as a unit
        assert april["clawback_amount"] == pytest.approx(168.66)  # 16,866 x 1% fallback rate
        assert april["net_commission"] == pytest.approx(0.0)

        (row,) = [c for c in periods["2026-04"]["client_rows"] if c["crm_id"] == "SOLO1"]
        assert row["unit_status"] == "clawback"
        assert row["client_name"] == "Shelleen Roseborough"
        assert row["clawback_amount"] == pytest.approx(168.66)

    def test_solo_low_credit_reclassified_client_still_shows_up(self):
        """The one reclassification path that's still real (unaffected by the
        payment-proof policy change above): a Credit Score <= 500 client
        earns zero commission, so there's nothing to claw back even though
        the row otherwise looks like a clawback. Regression coverage for the
        Step 3.5 visibility fix's ONE remaining trigger — if this client is
        their agent's only activity that month, the record must still show
        up (as same_month_cancel, $0), not vanish."""
        data = crm_csv([
            client("SOLO2", cleared="04/10/2026", dropped="07/23/2026",
                   payments="1", debt="16866", credit_score="450"),
        ])
        periods = by_period(parse_crm_and_calculate(data, "f.csv"))

        assert "2026-04" in periods
        (april,) = periods["2026-04"]["results"]
        assert april["units_cleared"] == 0
        assert april["clawback_amount"] == pytest.approx(0.0)

        (row,) = [c for c in periods["2026-04"]["client_rows"] if c["crm_id"] == "SOLO2"]
        assert row["unit_status"] == "same_month_cancel"

    def test_reclassification_visibility_fix_does_not_resurrect_a_genuine_clawback(self):
        """The visibility fix above must not undo the "latest period in file"
        policy: a genuinely proven-paid clawback client still must NOT get a
        separate entry at their own original cleared period, even if the fix
        runs. Same shape as
        test_clawback_from_earlier_cleared_month_lands_in_latest_period."""
        data = crm_csv([
            client("B1", cleared="05/10/2026", debt="20000"),
            client("OLD1", cleared="03/05/2026", dropped="06/23/2026",
                   payments="1", debt="10000"),
        ])
        periods = by_period(parse_crm_and_calculate(
            data, "f.csv", already_cleared_crm_ids={"B1", "OLD1"}))
        assert "2026-03" not in periods
        assert "2026-06" not in periods
        may = periods["2026-05"]["results"][0]
        assert may["clawback_amount"] == pytest.approx(100.0)

    def test_multiple_solo_clawback_clients_sharing_a_period_get_one_entry(self):
        """Two clients, same agent, same cleared month, no other activity that
        month, both genuine clawbacks (Step 4's target-period holding entry) —
        must produce exactly one result entry for that (agent, period),
        containing both, not a crash or duplicate entries."""
        data = crm_csv([
            client("X1", cleared="04/05/2026", dropped="07/10/2026", payments="1", debt="10000"),
            client("X2", cleared="04/06/2026", dropped="07/11/2026", payments="1", debt="10000"),
        ])
        periods = by_period(parse_crm_and_calculate(data, "f.csv"))
        assert "2026-04" in periods
        assert len(periods["2026-04"]["results"]) == 1
        assert periods["2026-04"]["results"][0]["units_cleared"] == 0
        assert periods["2026-04"]["results"][0]["clawback_amount"] == pytest.approx(200.0)  # 2 x 10,000 x 1%
        crm_ids = {c["crm_id"] for c in periods["2026-04"]["client_rows"]}
        assert crm_ids == {"X1", "X2"}

    def test_multiple_solo_low_credit_clients_sharing_a_period_get_one_entry(self):
        """Same shape as above, but for the one reclassification path that's
        still real: two low-credit clients, same agent, same month, no other
        activity — the Step 3.5 visibility-fix holding entry must handle
        more than one reclassified client sharing a key without duplicating
        or dropping either."""
        data = crm_csv([
            client("Y1", cleared="04/05/2026", dropped="07/10/2026", payments="1", credit_score="400"),
            client("Y2", cleared="04/06/2026", dropped="07/11/2026", payments="1", credit_score="450"),
        ])
        periods = by_period(parse_crm_and_calculate(data, "f.csv"))
        assert "2026-04" in periods
        assert len(periods["2026-04"]["results"]) == 1
        assert periods["2026-04"]["results"][0]["units_cleared"] == 0
        assert periods["2026-04"]["results"][0]["clawback_amount"] == pytest.approx(0.0)
        crm_ids = {c["crm_id"] for c in periods["2026-04"]["client_rows"]}
        assert crm_ids == {"Y1", "Y2"}
        statuses = {c["unit_status"] for c in periods["2026-04"]["client_rows"]}
        assert statuses == {"same_month_cancel"}

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
    """Late activation (reassigning a client's commission credit forward to
    the latest period in the file when their crm_id had never been seen
    before) was REMOVED — OWNER POLICY, confirmed August 2026. A client with
    a real 1st Payment Cleared Date is now always assumed to have been
    genuinely paid for their own real cleared month, whether or not this
    portal's own upload history happens to already have a record of them.
    This exact bug surfaced in production: a client cleared ~9 months
    earlier, with a normal ongoing payment history, got swept into the
    current month's commission purely because no prior upload/backfill for
    that agent existed yet in this portal — not because they were ever
    actually held pending."""

    def test_client_always_credited_in_their_own_cleared_month_with_partial_history(self):
        """The specific loophole that used to trigger late activation even
        with the "fresh DB" guard in place: SOME history exists in the DB
        (B1 is known), but not for A1 specifically. A1 must still be
        credited in May, their own real cleared month — never reassigned
        into June just because this crm_id is new to the system."""
        data = crm_csv([
            client("A1", cleared="05/10/2026"),
            client("B1", cleared="06/10/2026"),
        ])
        periods = by_period(parse_crm_and_calculate(
            data, "f.csv", already_cleared_crm_ids={"B1"}))
        assert set(periods) == {"2026-05", "2026-06"}
        assert periods["2026-05"]["results"][0]["units_cleared"] == 1
        assert periods["2026-06"]["results"][0]["units_cleared"] == 1
        (row,) = [c for c in periods["2026-05"]["client_rows"] if c["crm_id"] == "A1"]
        assert not row.get("is_late_activation")

    def test_client_always_credited_in_their_own_cleared_month_on_fresh_db(self):
        """Same invariant with NO history at all (first-ever upload) — kept
        as its own test since this exact case broke once before by a
        different mechanism (see CLAUDE.md's note on the old fresh-DB guard)."""
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


class TestCreditScore:
    """Credit Score (owner decision, July 2026, replaces the earlier Cordoba
    EPF-tab-matching mechanism): a client who clears with Credit Score <= 500 still
    counts as a full unit toward the agent's tier, but earns zero commission —
    their debt is excluded from total_cleared_debt entirely, individually and in
    aggregate. This is decided directly from the CRM row itself, so there's no
    cross-file ordering to worry about."""

    def test_low_credit_client_counts_as_unit_with_zero_commission(self):
        # 20 real cleared units at $5,000 each = $100,000, plus a 21st client with
        # Credit Score 500 and $5,000 debt that must NOT count toward the dollar total.
        rows = [client(f"A{i}", cleared="06/05/2026", debt="5000") for i in range(20)]
        rows.append(client("LC1", cleared="06/05/2026", debt="5000",
                            name="Low Credit Client", credit_score="500"))
        data = crm_csv(rows)
        periods = by_period(parse_crm_and_calculate(data, "f.csv"))
        result = periods["2026-06"]["results"][0]

        assert result["units_cleared"] == 21               # counts as a real unit
        assert result["total_cleared_debt"] == 100_000.0   # their $5,000 excluded
        assert result["raw_tier"] == 2                     # 21 units -> Tier 2
        assert result["tier_rate"] == 0.0125
        assert result["gross_commission"] == 1_250.0       # 100,000 x 1.25%
        assert "1 unit(s) counted at $0 commission" in result["notes"]

        lc_row = next(c for c in periods["2026-06"]["client_rows"] if c["crm_id"] == "LC1")
        assert lc_row["is_low_credit"] is True
        assert lc_row["commission_on_client"] == 0.0
        assert lc_row["is_cleared"] is True

    def test_credit_score_above_500_is_not_low_credit(self):
        data = crm_csv([client("A1", cleared="06/05/2026", debt="5000", credit_score="501")])
        periods = by_period(parse_crm_and_calculate(data, "f.csv"))
        result = periods["2026-06"]["results"][0]
        assert result["total_cleared_debt"] == 5_000.0
        assert result["gross_commission"] == pytest.approx(50.0)  # normal 1% Tier 1
        row = periods["2026-06"]["client_rows"][0]
        assert row["is_low_credit"] is False
        assert row["commission_on_client"] == pytest.approx(50.0)

    def test_missing_credit_score_is_not_low_credit(self):
        data = crm_csv([client("A1", cleared="06/05/2026", debt="5000")])
        periods = by_period(parse_crm_and_calculate(data, "f.csv"))
        result = periods["2026-06"]["results"][0]
        assert result["total_cleared_debt"] == 5_000.0
        row = periods["2026-06"]["client_rows"][0]
        assert row["is_low_credit"] is False

    def test_low_credit_client_dropping_later_triggers_no_clawback(self):
        """A low-credit client was never paid any commission, so a later drop must
        not claw back money that was never sent — same file, cleared then dropped
        in a later month, below the safe threshold, on/after the payout date."""
        data = crm_csv([
            client("A1", cleared="06/10/2026", debt="20000"),
            client("LC1", cleared="06/12/2026", dropped="08/03/2026", payments="1",
                   debt="10000", credit_score="450"),
        ])
        periods = by_period(parse_crm_and_calculate(
            data, "f.csv", already_cleared_crm_ids={"A1", "LC1"}))
        assert "2026-08" not in periods   # no holding entry created — nothing clawed back

    def test_low_credit_client_known_from_prior_upload_triggers_no_clawback(self):
        """Same guard, but the low-credit flag comes from the DB (already_low_credit_crm_ids)
        because the client cleared in a prior upload, not this file."""
        data = crm_csv([
            client("A1", cleared="06/10/2026", debt="20000"),
            client("LC1", cleared="06/12/2026", dropped="08/03/2026", payments="1", debt="10000"),
        ])
        periods = by_period(parse_crm_and_calculate(
            data, "f.csv",
            already_cleared_crm_ids={"A1", "LC1"},
            already_low_credit_crm_ids={"LC1"},
        ))
        assert "2026-08" not in periods
