from .utils import get_organization


def organization_context(request):
    """Inject org_name into every template context.

    Uses the session-cached get_organization() so this adds zero DB queries
    after the first request in a session.
    """
    org = get_organization(request)
    return {
        'org_name': org.name if org else 'Eventflow',
    }
