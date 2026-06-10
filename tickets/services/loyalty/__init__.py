"""Loyalty program services: tier assignment + dashboard stats."""
from .stats import LoyaltyProgramStats
from .tier_assigner import LoyaltyTierAssigner


def assign_loyalty_tiers(program):
    """Recompute tier membership for every customer in the program's org."""
    return LoyaltyTierAssigner(program).calculate_all()


__all__ = [
    "LoyaltyProgramStats",
    "LoyaltyTierAssigner",
    "assign_loyalty_tiers",
]
