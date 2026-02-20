from .utils import get_organization
from .feature_flags import direct_ticketing_enabled as direct_ticketing_enabled_flag


def organization_context(request):
    """Inject org_name, is_organizer, is_attendee, and user_role into every template context.

    Uses the session-cached get_organization() so this adds zero DB queries
    after the first request in a session.
    """
    org = get_organization(request)
    ctx = {
        'org_name': org.name if org else 'Eventflow',
        'user_role': 'organizer',
        'is_organizer': True,
        'is_attendee': False,
    }
    if request.user.is_authenticated:
        try:
            profile = request.user.profile
            ctx['user_role']    = profile.role
            ctx['is_organizer'] = profile.is_organizer
            ctx['is_attendee']  = profile.is_attendee
        except Exception:
            pass
    return ctx


def feature_flags_context(request):
    """Inject feature flag values into every template context."""
    return {
        'direct_ticketing_enabled': direct_ticketing_enabled_flag(request.user),
    }
