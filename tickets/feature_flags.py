"""Feature flags for gating functionality by user."""

from .models import FeatureFlagSettings


def _global_feature_flags():
    return FeatureFlagSettings.get_solo()


def smart_pricing_recommendations_enabled(user):
    """Return True when Smart Pricing Recommendations are enabled for the user."""
    if not user or not getattr(user, 'is_authenticated', False) or not user.is_authenticated:
        return False
    if not getattr(user, 'is_superuser', False):
        return False
    return _global_feature_flags().smart_pricing_recommendations_enabled


def waitlist_enabled(organization):
    """Return True if the organization has the waitlist feature enabled."""
    if organization is None:
        return False
    return bool(organization.waitlist_feature_enabled)


def loyalty_enabled(organization):
    """Return True if the organization has the loyalty program feature enabled."""
    if organization is None:
        return False
    return bool(organization.loyalty_feature_enabled)


def external_events_enabled(organization):
    """Return True if the organization may create external (CSV) events."""
    if organization is None:
        return False
    return bool(organization.external_events_enabled)


def instagram_feature_enabled(organization):
    """Return True if the org can see/use the Instagram integration (rollout gate):
    connection settings + organic content analytics."""
    if organization is None:
        return False
    return bool(organization.instagram_feature_enabled)


def instagram_dm_agent_enabled(organization):
    """Return True if the Instagram DM support agent (inbox, FAQs, auto-answer) is on
    for the org: the global INSTAGRAM_DM_AGENT_ENABLED switch AND the org's Instagram
    rollout gate."""
    from django.conf import settings

    if not getattr(settings, 'INSTAGRAM_DM_AGENT_ENABLED', False):
        return False
    return instagram_feature_enabled(organization)


def browse_events_enabled():
    """Return True to make the public Browse Events page accessible."""
    return _global_feature_flags().browse_events_enabled
