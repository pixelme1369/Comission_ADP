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

# Per-agent contractual overrides that bypass the tier table entirely (deal
# negotiated directly with the CEO, outside the standard commission plan).
# Rate applies unconditionally — the cancellation-rate tier-drop penalty
# never touches it, and it's reused as-is for clawback math on that agent's
# clients so a clawed-back client's rate matches what they were actually paid.
AGENT_FIXED_RATES = {
    "alex tambouly": 0.02,
}


def get_fixed_rate(agent_name: str):
    """Return the contractual fixed rate for an agent, or None if they're on the standard tier table."""
    return AGENT_FIXED_RATES.get((agent_name or "").strip().lower())


def get_tier(units: int) -> tuple:
    """Return (tier_number, rate, label) for given units cleared."""
    if units < 1:
        # Without this guard, anything below 1 falls through to the open-ended
        # 61+ tier ("high is None") and silently earns the TOP rate.
        raise ValueError(f"Units {units} out of valid range (must be >= 1)")
    for i, (low, high, rate, label) in enumerate(TIERS, start=1):
        if low <= units and (high is None or units <= high):
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

    fixed_rate = get_fixed_rate(agent_name)
    if fixed_rate is not None:
        # Contractual fixed rate overrides the tier table unconditionally —
        # the cancellation-rate tier-drop penalty does not apply.
        penalty_applied = False
        adjusted_tier_num = raw_tier_num
        tier_rate = fixed_rate
        tier_label = "Fixed Rate (contract)"

    gross_commission = tier_rate * total_cleared_debt

    # Draw vs commission: agent gets whichever is higher; draw is non-recoverable
    if gross_commission > hourly_draw:
        payout = gross_commission
        payout_type = "commission"
    else:
        payout = hourly_draw
        payout_type = "draw"

    quality_bonus_eligible = cancellation_rate_pct < QUALITY_BONUS_THRESHOLD

    if fixed_rate is not None:
        notes_parts = [f"Fixed rate {tier_rate*100:.2f}% (contract override, tier table not applied)"]
    else:
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


def get_adjusted_tier_rate(units: int, cancellation_rate_pct: float, agent_name: str = None) -> tuple:
    """Return (adjusted_tier_num, rate) for a unit count, applying the cancellation penalty.

    If agent_name has a contractual fixed rate, that rate is returned unconditionally
    (no tier lookup, no cancellation penalty).
    """
    fixed_rate = get_fixed_rate(agent_name)
    if fixed_rate is not None:
        raw_tier_num, _, _ = get_tier(units) if units > 0 else (0, 0.0, "")
        return raw_tier_num, fixed_rate
    if units <= 0:
        return 0, 0.0
    raw_tier_num, _, _ = get_tier(units)
    penalty_applied = cancellation_rate_pct > CANCELLATION_PENALTY_THRESHOLD
    adjusted_tier_num = max(1, raw_tier_num - 1) if penalty_applied else raw_tier_num
    _, _, adjusted_rate, _ = TIERS[adjusted_tier_num - 1]
    return adjusted_tier_num, adjusted_rate


def calculate_clawback_amount(
    orig_units: int,
    orig_total_debt: float,
    orig_gross_commission: float,
    orig_cancellation_rate_pct: float,
    client_debt: float,
    agent_name: str = None,
) -> float:
    """
    Clawback owed for removing one already-commissioned client from a month's totals.

    If it was the agent's only cleared unit that month, the full commission is clawed
    back. If removing the client drops the agent's tier, the clawback is the full
    commission difference on the whole month's debt (not just this client's share).
    If the tier is unchanged, the clawback is just this client's share of the rate.

    If agent_name has a contractual fixed rate, the tier never changes (there is no
    tier), so the clawback is always just this client's share at that fixed rate.
    """
    if orig_units <= 1:
        return round(orig_gross_commission, 2)

    fixed_rate = get_fixed_rate(agent_name)
    if fixed_rate is not None:
        return max(0.0, round(client_debt * fixed_rate, 2))

    new_units = orig_units - 1
    new_debt = orig_total_debt - client_debt
    _, new_rate = get_adjusted_tier_rate(new_units, orig_cancellation_rate_pct)
    _, orig_rate = get_adjusted_tier_rate(orig_units, orig_cancellation_rate_pct)

    if new_rate != orig_rate:
        new_commission = new_rate * new_debt
        cb = orig_gross_commission - new_commission
    else:
        cb = client_debt * orig_rate

    return max(0.0, round(cb, 2))
