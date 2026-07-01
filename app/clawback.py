"""
Clawback engine.

A clawback triggers when:
  - A client's 1st Payment Cleared Date was in Month A (already paid)
  - The client's Dropped Date falls in Month B (current upload, a later month)
  - The client has Payments Made < 3

Clawback calculation:
  1. Find the agent's AgentCommission record for Month A
  2. Remove the cancelled client's unit from Month A's cleared unit count
  3. Recalculate the tier for Month A with reduced units (applying same cancellation
     penalty logic if the cancel rate also changes)
  4. Clawback = what was paid in Month A − what should have been paid at the lower tier
     across ALL of Month A's cleared debt (tier change affects everyone)
  5. If tier doesn't change, clawback = just the commission on that one client
     (enrolled_debt * Month A tier_rate)

The total clawback for the agent is deducted from Month B's net_commission.
"""

from app.calculator import get_tier, TIERS, CANCELLATION_PENALTY_THRESHOLD


def _get_adjusted_rate(units: int, cancel_rate_pct: float) -> tuple:
    """Return (adjusted_tier_num, tier_rate) applying cancellation penalty."""
    if units <= 0:
        return 0, 0.0
    raw_tier, _, _ = get_tier(units)
    penalty = cancel_rate_pct > CANCELLATION_PENALTY_THRESHOLD
    adj_tier = max(1, raw_tier - 1) if penalty else raw_tier
    _, _, rate, _ = TIERS[adj_tier - 1]
    return adj_tier, rate


def calculate_clawbacks(agent_commission_record, cancelled_clients_this_period, db_session):
    """
    Given an AgentCommission for Month B and a list of ClientRecord objects
    that were cancelled this period (Dropped Date in Month B, cleared in prior months,
    payments_made < 3), compute the total clawback and per-client clawback amounts.

    Returns:
        total_clawback (float)
        list of (client_record, clawback_amount) tuples
    """
    from app.models import AgentCommission, ClientRecord

    results = []
    total_clawback = 0.0

    for client in cancelled_clients_this_period:
        # Find the original AgentCommission record for the month the client cleared
        original_period_label = client.cleared_period
        if not original_period_label:
            continue

        original_ac = (
            AgentCommission.query
            .join(AgentCommission.period)
            .filter(
                AgentCommission.agent_name == client.agent_name,
            )
            .filter(
                __import__('app').models.CommissionPeriod.period_label == original_period_label
            )
            .first()
        )

        if not original_ac:
            # Commission record not found — might not have been imported yet
            # Fall back: clawback = client's debt * original tier rate stored on client
            clawback = client.commission_on_client
            results.append((client, clawback))
            total_clawback += clawback
            continue

        # How many units did the agent originally have in Month A?
        original_units = original_ac.units_cleared
        original_cancel_rate = original_ac.cancellation_rate
        original_total_debt = original_ac.total_cleared_debt
        original_commission = original_ac.gross_commission

        if original_units <= 1:
            # Removing this unit leaves 0 cleared — full commission is clawed back
            clawback = original_commission
        else:
            new_units = original_units - 1
            new_debt = original_total_debt - client.enrolled_debt

            # Recalculate cancel rate: the cancelled client was already in cancelled count,
            # removing them from cleared count slightly changes the rate.
            # For simplicity use the stored cancel rate (conservative).
            _, new_rate = _get_adjusted_rate(new_units, original_cancel_rate)
            _, original_rate = _get_adjusted_rate(original_units, original_cancel_rate)

            if new_rate != original_rate:
                # Tier changed — clawback is the full difference on all of Month A's debt
                new_commission = new_rate * new_debt
                clawback = original_commission - new_commission
            else:
                # Same tier — clawback is just this client's share
                clawback = client.enrolled_debt * original_rate

        clawback = max(0.0, round(clawback, 2))
        results.append((client, clawback))
        total_clawback += clawback

    return round(total_clawback, 2), results
