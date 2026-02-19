"""Feature flags for gating functionality by user."""

DIRECT_TICKETING_ALLOWED_EMAIL = 'owen@familiarfaces.la'


def direct_ticketing_enabled(user):
    """Return True only for the allowed user (owen@familiarfaces.la)."""
    if not user or not getattr(user, 'is_authenticated', False) or not user.is_authenticated:
        return False
    email = (getattr(user, 'email', None) or '').strip().lower()
    return email == DIRECT_TICKETING_ALLOWED_EMAIL.lower()
