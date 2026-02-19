from .utils import get_organization
from .feature_flags import direct_ticketing_enabled as direct_ticketing_enabled_flag


def organization_context(request):
    """Inject org_name into every template context.

    Uses the session-cached get_organization() so this adds zero DB queries
    after the first request in a session.
    """
    org = get_organization(request)
    return {
        'org_name': org.name if org else 'Eventflow',
    }


def feature_flags_context(request):
    """Inject feature flag values into every template context."""
    return {
        'direct_ticketing_enabled': direct_ticketing_enabled_flag(request.user),
    }
