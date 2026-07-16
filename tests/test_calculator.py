"""Locks in the April 2026 commission plan exactly as documented in CLAUDE.md.
If any of these fail, the money math changed — do not ship without owner sign-off."""

import pytest

from app.calculator import (
    calculate_agent_commission,
    calculate_clawback_amount,
    get_tier,
)


class TestTierTable:
    @pytest.mark.parametrize("units,tier,rate", [
        (1, 1, 0.0100), (20, 1, 0.0100),
        (21, 2, 0.0125), (31, 2, 0.0125),
        (32, 3, 0.0150), (39, 3, 0.0150),
        (40, 4, 0.0175), (45, 4, 0.0175),
        (46, 5, 0.0200), (60, 5, 0.0200),   # 60 units is still Tier 5
        (61, 6, 0.0225), (500, 6, 0.0225),
    ])
    def test_boundaries(self, units, tier, rate):
        got_tier, got_rate, _ = get_tier(units)
        assert (got_tier, got_rate) == (tier, rate)

    def test_zero_units_invalid(self):
        with pytest.raises(ValueError):
            get_tier(0)


class TestCommission:
    def test_gross_is_rate_times_total_debt(self):
        r = calculate_agent_commission("A", 25, 500_000.0, 0.0)
        assert r["adjusted_tier"] == 2
        assert r["gross_commission"] == pytest.approx(6_250.0)

    def test_penalty_is_strictly_above_20(self):
        assert calculate_agent_commission("A", 25, 100_000, 20.0)["cancellation_penalty_applied"] is False
        assert calculate_agent_commission("A", 25, 100_000, 20.01)["cancellation_penalty_applied"] is True

    def test_penalty_drops_exactly_one_tier(self):
        r = calculate_agent_commission("A", 25, 500_000.0, 25.0)
        assert (r["raw_tier"], r["adjusted_tier"]) == (2, 1)
        assert r["gross_commission"] == pytest.approx(5_000.0)  # 1% instead of 1.25%

    def test_penalty_never_drops_below_tier_1(self):
        r = calculate_agent_commission("A", 5, 100_000.0, 50.0)
        assert r["adjusted_tier"] == 1

    def test_quality_bonus_is_strictly_below_10(self):
        assert calculate_agent_commission("A", 25, 100_000, 9.99)["quality_bonus_eligible"] is True
        assert calculate_agent_commission("A", 25, 100_000, 10.0)["quality_bonus_eligible"] is False

    def test_draw_wins_when_commission_is_lower(self):
        r = calculate_agent_commission("A", 1, 10_000.0, 0.0, hourly_draw=500.0)
        assert r["payout_type"] == "draw"
        assert r["payout"] == 500.0

    def test_commission_wins_when_higher_than_draw(self):
        r = calculate_agent_commission("A", 25, 500_000.0, 0.0, hourly_draw=500.0)
        assert r["payout_type"] == "commission"
        assert r["payout"] == pytest.approx(6_250.0)


class TestClawbackAmount:
    def test_only_unit_claws_back_full_commission(self):
        assert calculate_clawback_amount(1, 30_000, 300.0, 0.0, 30_000) == 300.0

    def test_no_tier_change_claws_back_client_share_only(self):
        # 25 units -> 24 units: both Tier 2, so only the client's own share
        cb = calculate_clawback_amount(25, 500_000.0, 6_250.0, 0.0, 30_000.0)
        assert cb == pytest.approx(375.0)  # 30,000 x 1.25%

    def test_tier_change_claws_back_difference_on_whole_month(self):
        # 21 units (Tier 2) -> 20 units (Tier 1): whole month repriced
        cb = calculate_clawback_amount(21, 420_000.0, 5_250.0, 0.0, 20_000.0)
        # new commission = 400,000 x 1% = 4,000 ; clawback = 5,250 - 4,000
        assert cb == pytest.approx(1_250.0)

    def test_never_negative(self):
        assert calculate_clawback_amount(2, 10_000.0, 0.0, 0.0, 5_000.0) >= 0.0
