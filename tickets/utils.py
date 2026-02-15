"""Organization helpers for scoping data by current user's organization."""

from functools import wraps

from django.shortcuts import redirect


def get_organization(request):
    """
    Return the current user's organization, or None if not authenticated or no org.

    Ensures UserProfile exists for the user (creates one with organization=None if missing).
    """
    if not request.user.is_authenticated:
        return None
    from .models import UserProfile
    profile, _ = UserProfile.objects.get_or_create(
        user=request.user,
        defaults={'organization_id': None},
    )
    return profile.organization


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
