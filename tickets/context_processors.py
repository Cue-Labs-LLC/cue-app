from .utils import get_organization
from .feature_flags import direct_ticketing_enabled as direct_ticketing_enabled_flag


def organization_context(request):
    """Inject org_name, is_organizer, is_attendee, user_role, and view_mode into every template context.

    Uses the session-cached get_organization() so this adds zero DB queries
    after the first request in a session.
    """
    org = get_organization(request)
    ctx = {
        'org_name': org.name if org else 'Eventflow',
        'user_role': 'organizer',
        'is_organizer': True,
        'is_attendee': False,
        'view_mode': 'organizer',
    }
    if request.user.is_authenticated:
        try:
            profile = request.user.profile
            ctx['user_role'] = profile.role          # actual DB role — never overridden
            if profile.is_organizer:
                view_mode = request.session.get('_view_mode', 'organizer')
                ctx['view_mode'] = view_mode
                if view_mode == 'attendee':
                    ctx['is_organizer'] = False
                    ctx['is_attendee'] = True
                else:
                    ctx['is_organizer'] = True
                    ctx['is_attendee'] = profile.is_attendee
            else:
                ctx['is_organizer'] = False
                ctx['is_attendee'] = profile.is_attendee
                ctx['view_mode'] = 'attendee'
        except Exception:
            pass
    return ctx


def feature_flags_context(request):
    """Inject feature flag values into every template context."""
    return {
        'direct_ticketing_enabled': direct_ticketing_enabled_flag(request.user),
    }
