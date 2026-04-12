from .behavior_profiles import (
    BEHAVIOR_PROFILE_BADGE_COLORS,
    BEHAVIOR_PROFILE_DESCRIPTIONS,
    BEHAVIOR_PROFILE_ORDER,
    CustomerBehaviorProfiler,
)
from .rfm_calculator import RFMCalculator


def recalculate_customer_segments(organization):
    """Recompute both RFM and behavior-profile fields for an organization."""
    RFMCalculator(organization).calculate_all()
    CustomerBehaviorProfiler(organization).calculate_all()


__all__ = [
    "BEHAVIOR_PROFILE_BADGE_COLORS",
    "BEHAVIOR_PROFILE_DESCRIPTIONS",
    "BEHAVIOR_PROFILE_ORDER",
    "CustomerBehaviorProfiler",
    "RFMCalculator",
    "recalculate_customer_segments",
]
