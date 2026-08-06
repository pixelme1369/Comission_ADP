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
    "peter godwin": 0.0175,
}


def get_fixed_rate(agent_name: str):
    """Return the contractual fixed rate for an agent, or None if they're on the standard tier table."""
    return AGENT_FIXED_RATES.get((agent_name or "").strip().lower())


def agent_identity_key(agent_name: str) -> str:
    """Case/whitespace-insensitive identity key for an agent name.

    The CRM export has no stable per-agent ID — "Sales Rep" is a free-text
    column — and the same real person can genuinely appear under different
    casings, both across separate files AND within the SAME file (confirmed
    real case: "amir moayeri" and "Amir Moayeri" both appearing in one CRM
    export). Use this wherever agent identity needs to be MATCHED — grouping
    rows within a parse (see canonicalize_agent_names below), matching a
    clawback's original period against the DB (known_period_totals) — never
    for display, where the actually-observed spelling should be kept.
    """
    return (agent_name or "").strip().lower()


def build_canonical_agent_name_map(raw_names) -> dict:
    """Given every raw agent-name spelling observed in one file (repeats
    included — frequency is part of the tie-break), returns
    {agent_identity_key(raw): canonical_spelling}.

    Canonical spelling = whichever raw spelling occurs most often; ties
    prefer a spelling that isn't ALL-lowercase (reads better than a free-text
    field's all-lowercase entry), then first-seen order. Never invents a
    spelling that wasn't actually present in `raw_names`. See
    agent_identity_key's docstring for why this collapsing is needed at all.
    """
    seen_order = []
    counts = {}
    for raw in raw_names:
        raw = (raw or "").strip()
        if not raw:
            continue
        if raw not in counts:
            counts[raw] = 0
            seen_order.append(raw)
        counts[raw] += 1

    canonical_by_key = {}
    for raw in seen_order:
        key = agent_identity_key(raw)
        current = canonical_by_key.get(key)
        if current is None:
            canonical_by_key[key] = raw
            continue
        # Strict improvement only, so first-seen naturally wins remaining ties.
        challenger_rank = (counts[raw], raw != raw.lower())
        current_rank = (counts[current], current != current.lower())
        if challenger_rank > current_rank:
            canonical_by_key[key] = raw
    return canonical_by_key


def canonicalize_agent_names(rows, name_key="agent_name"):
    """Rewrites rows[i][name_key] in place so every raw spelling sharing the
    same agent_identity_key() collapses to ONE canonical spelling across this
    batch of rows (see build_canonical_agent_name_map for how the canonical
    spelling is picked).

    Without this, two casings of the same real agent appearing in the SAME
    file (see agent_identity_key's docstring) would silently split their
    production into two separate, artificially smaller tier/commission
    calculations for what should be one agent's one period — tier is a
    function of TOTAL units cleared, and a casing typo in a free-text column
    is not a second agent.
    """
    canonical_by_key = build_canonical_agent_name_map(row.get(name_key) for row in rows)
    for row in rows:
        raw = (row.get(name_key) or "").strip()
        if raw:
            row[name_key] = canonical_by_key[agent_identity_key(raw)]


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


def units_to_next_tier(units_cleared: int, agent_name: str = None) -> int | None:
    """Units still needed this period to reach the next tier's threshold.

    Returns None if the agent has a contractual fixed rate (AGENT_FIXED_RATES) —
    the tier table doesn't apply to them, so there's no "next tier" to chase — or if
    they're already in the top tier (61+, no ceiling to climb toward).
    """
    if get_fixed_rate(agent_name) is not None:
        return None
    if units_cleared < 1:
        return TIERS[0][0] - units_cleared
    tier_num, _, _ = get_tier(units_cleared)
    if tier_num >= len(TIERS):
        return None
    next_low = TIERS[tier_num][0]
    return next_low - units_cleared


def commission_gain_at_next_tier(
    adjusted_tier: int,
    total_cleared_debt: float,
    gross_commission: float,
    agent_name: str = None,
) -> float | None:
    """Illustrative "tier up and earn this much more" figure for the agent dashboard:
    what gross commission would be on this period's SAME total_cleared_debt at the
    next tier's rate, minus what was actually earned. Purely a motivational display
    number, not a payout calculation — hitting the next tier in reality means more
    cleared debt too, which this simplification doesn't model.

    Returns None under the same conditions as units_to_next_tier: a contractual
    fixed-rate agent (no tier to chase), or already at the top tier.
    """
    if get_fixed_rate(agent_name) is not None:
        return None
    if adjusted_tier < 1 or adjusted_tier >= len(TIERS):
        return None
    next_rate = TIERS[adjusted_tier][2]  # TIERS[adjusted_tier] is the next tier (0-indexed)
    potential_gross = total_cleared_debt * next_rate
    return round(potential_gross - gross_commission, 2)


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

    OWNER POLICY (revised, supersedes the original "tier recalculation" rule that
    used to live here): the clawback is always just this client's own proportional
    share of whatever rate the ORIGINAL month was actually paid at —
    client_debt × orig_rate, where orig_rate = orig_gross_commission / orig_total_debt
    (the exact effective rate that period's commission was calculated at — the
    standard tier table, a fixed-rate agent, or a blended rate summed across
    multiple sources sharing a period_label, see known_period_totals in
    crm_parser.py — all collapse to the same gross/debt ratio either way, so this
    needs no separate case for any of them).

    This REPLACES the previous rule ("if removing this client would have dropped
    the whole month below a tier boundary, claw back the full commission
    difference across ALL of that month's debt, not just this client's share").
    That rule could produce a clawback wildly out of proportion to the dropped
    client's own debt — confirmed real case: an $18,007 client generating a
    $5,364.47 clawback (~30% effective rate, nowhere near any real tier rate)
    because their departure alone, in isolation, would have dropped the agent
    below a tier boundary for a month's worth of OTHER clients' debt they had
    nothing to do with. orig_units and orig_cancellation_rate_pct are no longer
    used by this function — kept as parameters only for backward compatibility
    with existing call sites (crm_parser.py, cordoba_ingest.py, routes.py); do
    not remove them without updating all of those.

    Fixed-rate agents (get_fixed_rate) are checked first and return their flat
    rate unconditionally, same as always — there's no tier to recalculate for them
    either way, before or after this policy change.
    """
    fixed_rate = get_fixed_rate(agent_name)
    if fixed_rate is not None:
        return max(0.0, round(client_debt * fixed_rate, 2))

    # No debt on record for the original month (orig_result wasn't found at all,
    # or the only other activity was a $0-commission low-credit/safe-cancel unit)
    # — fall back to the lowest tier rate, same fallback crm_parser.py's Step 3
    # already uses when it can't find an orig_result to pass in here at all.
    orig_rate = (orig_gross_commission / orig_total_debt) if orig_total_debt > 0 else 0.01

    return max(0.0, round(client_debt * orig_rate, 2))
