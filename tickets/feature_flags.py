"""Feature flags for gating functionality by user."""


def direct_ticketing_enabled(user):
    """Return True for any superuser."""
    if not user or not getattr(user, 'is_authenticated', False) or not user.is_authenticated:
        return False
    return getattr(user, 'is_superuser', False)
