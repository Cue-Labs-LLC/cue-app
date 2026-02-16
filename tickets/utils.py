"""Organization helpers for scoping data by current user's organization."""

from functools import wraps

from django.shortcuts import redirect


def get_organization(request):
    """
    Return the current user's organization, or None if not authenticated or no org.

    Caches the organization object on the request so that multiple calls in the
    same request (decorator + view body + context processor) cost zero DB queries
    after the first. The PK is also stored in the session for cross-request caching.
    """
    if not request.user.is_authenticated:
        return None

    # Per-request object cache — avoids repeated DB hits within the same request
    _attr = '_cached_org'
    _sentinel = '_cached_org_set'
    if getattr(request, _sentinel, False):
        return getattr(request, _attr, None)

    from .models import Organization, UserProfile

    org = None
    org_id = request.session.get('_org_id')
    if org_id is not None:
        # Fast path: org id cached in session
        if org_id == '':
            request._cached_org = None
            request._cached_org_set = True
            return None  # user has no org
        try:
            org = Organization.objects.get(pk=org_id)
        except Organization.DoesNotExist:
            # Stale session value — fall through to DB lookup
            org = None

    if org is None and org_id != '':
        # Slow path: DB lookup
        profile, _ = UserProfile.objects.select_related('organization').get_or_create(
            user=request.user,
            defaults={'organization_id': None},
        )
        org = profile.organization
        request.session['_org_id'] = str(org.pk) if org else ''

    request._cached_org = org
    request._cached_org_set = True
    return org


def clear_org_cache(request):
    """Remove the cached organization from the session and request.

    Call this when a user's org assignment changes (e.g. admin reassignment,
    org creation) so the next get_organization() re-fetches from the DB.
    """
    request.session.pop('_org_id', None)
    request._cached_org = None
    request._cached_org_set = False


def require_org(view_func):
    """
    Decorator that requires the user to have an organization.
    Redirects to org_required page if profile.organization is None.
    Superusers bypass (they can have no org and still access admin).
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(request.get_full_path())
        org = get_organization(request)
        if org is None and not request.user.is_superuser:
            return redirect('tickets:org_required')
        return view_func(request, *args, **kwargs)
    return _wrapped
