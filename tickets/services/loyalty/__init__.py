"""Loyalty program services: tier assignment, dashboard stats, points wallet."""
from .points import (
    award_points_for_order,
    award_points_for_orders,
    get_points_program,
    points_for_order,
    revoke_points_for_order,
    revoke_points_for_orders,
)
from .stats import LoyaltyProgramStats
from .tier_assigner import LoyaltyTierAssigner


def assign_loyalty_tiers(program):
    """Recompute tier membership for every customer in the program's org."""
    return LoyaltyTierAssigner(program).calculate_all()


__all__ = [
    "LoyaltyProgramStats",
    "LoyaltyTierAssigner",
    "assign_loyalty_tiers",
    "award_points_for_order",
    "award_points_for_orders",
    "get_points_program",
    "points_for_order",
    "revoke_points_for_order",
    "revoke_points_for_orders",
]
