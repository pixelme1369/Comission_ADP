TIERS = [
    (1,  20,  0.0100, "Tier 1"),
    (21, 31,  0.0125, "Tier 2"),
    (32, 39,  0.0150, "Tier 3"),
    (40, 45,  0.0175, "Tier 4 – President's Club"),
    (46, 60,  0.0200, "Tier 5 – Chairman's Club"),
    (61, None, 0.0225, "Tier 6 – Legacy Club"),
]

QUALITY_BONUS_AMOUNT = 500.00
CANCELLATION_PENALTY_THRESHOLD = 20.0  # > this triggers tier drop
QUALITY_BONUS_THRESHOLD = 10.0         # < this triggers bonus eligibility


def get_tier(units: int) -> tuple:
    """Return (tier_number, rate, label) for given units cleared."""
    for i, (low, high, rate, label) in enumerate(TIERS, start=1):
        if high is None or low <= units <= high:
            return i, rate, label
    raise ValueError(f"Units {units} out of valid range (must be >= 1)")


def calculate_agent_commission(
    agent_name: str,
    units_cleared: int,
    total_cleared_debt: float,
    cancellation_rate_pct: float,
    hourly_draw: float = 0.0,
) -> dict:
    """
    Calculate commission for a single agent for one month.
    cancellation_rate_pct is a percentage value (e.g. 18.5 means 18.5%).
    Returns a dict matching the AgentCommission model fields.
    """
    raw_tier_num, _, _ = get_tier(units_cleared)

    # Apply cancellation penalty: > 20% drops one tier
    penalty_applied = cancellation_rate_pct > CANCELLATION_PENALTY_THRESHOLD
    adjusted_tier_num = max(1, raw_tier_num - 1) if penalty_applied else raw_tier_num

    # Get rate for adjusted tier
    _, _high, tier_rate, tier_label = TIERS[adjusted_tier_num - 1]

    gross_commission = tier_rate * total_cleared_debt

    # Draw vs commission: agent gets whichever is higher; draw is non-recoverable
    if gross_commission > hourly_draw:
        payout = gross_commission
        payout_type = "commission"
    else:
        payout = hourly_draw
        payout_type = "draw"

    quality_bonus_eligible = cancellation_rate_pct < QUALITY_BONUS_THRESHOLD

    notes_parts = [f"Tier {adjusted_tier_num} ({tier_label}) @ {tier_rate*100:.2f}%"]
    if penalty_applied:
        notes_parts.append(f"Tier dropped from {raw_tier_num} due to cancellation rate {cancellation_rate_pct:.1f}% > 20%")
    if quality_bonus_eligible:
        notes_parts.append("Quality bonus rate eligible (< 10% cancellations) — pending manual review")
    if payout_type == "draw":
        notes_parts.append("Commission below draw; agent receives hourly draw")

    return {
        "agent_name": agent_name,
        "units_cleared": units_cleared,
        "total_cleared_debt": total_cleared_debt,
        "cancellation_rate": cancellation_rate_pct,
        "hourly_draw": hourly_draw,
        "raw_tier": raw_tier_num,
        "adjusted_tier": adjusted_tier_num,
        "tier_rate": tier_rate,
        "gross_commission": gross_commission,
        "payout": payout,
        "payout_type": payout_type,
        "quality_bonus_eligible": quality_bonus_eligible,
        "cancellation_penalty_applied": penalty_applied,
        "notes": " | ".join(notes_parts),
    }
