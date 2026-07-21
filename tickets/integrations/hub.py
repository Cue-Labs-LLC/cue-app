"""The Integrations hub — a single page that lists every integration and its
live connection status, reached from Settings.
"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from ..utils import get_organization, require_org, require_admin
from .registry import integration_statuses


@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def integrations_overview(request):
    """List all integrations with Connected / Not connected status for this org."""
    org = get_organization(request)
    integrations = integration_statuses(
        org, include_superuser_only=request.user.is_superuser
    )
    return render(request, 'tickets/integrations_overview.html', {
        'integrations': integrations,
    })
