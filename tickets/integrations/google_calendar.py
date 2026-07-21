"""Google Calendar integration views.

Moved out of tickets/views.py into the integrations package so the
integration is referenced rather than built into the core app.
Shared helpers remain in tickets.views and are imported below.
"""

from ..models import PipedreamCalendarConnection
from django.db import connection
from ..utils import get_organization
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import redirect
from django.shortcuts import render
from ..utils import require_admin
from ..utils import require_org


@login_required
@require_org
@require_admin
def settings_google_calendar(request):
    """Google Calendar (Pipedream) settings: set or edit webhook URL, or disconnect."""
    from django.core.validators import URLValidator
    from django.core.exceptions import ValidationError

    org = get_organization(request)
    connection = PipedreamCalendarConnection.objects.filter(organization=org).first()

    if request.method == 'POST':
        webhook_url = (request.POST.get('webhook_url') or '').strip()
        if not webhook_url:
            messages.error(request, 'Please enter a webhook URL.')
            return redirect('tickets:settings_google_calendar')
        try:
            URLValidator()(webhook_url)
        except ValidationError:
            messages.error(request, 'Please enter a valid URL.')
            return redirect('tickets:settings_google_calendar')
        connection, created = PipedreamCalendarConnection.objects.get_or_create(
            organization=org,
            defaults={'webhook_url': webhook_url},
        )
        if not created:
            connection.webhook_url = webhook_url
            connection.save()
        messages.success(request, 'Google Calendar (Pipedream) webhook saved. New events will be sent to this URL.')
        return redirect('tickets:settings_google_calendar')

    context = {
        'connection': connection,
    }
    return render(request, 'tickets/settings_google_calendar.html', context)


@login_required
@require_org
@require_admin
def settings_google_calendar_disconnect(request):
    """Remove Pipedream calendar connection for the current org."""
    if request.method != 'POST':
        return redirect('tickets:settings_google_calendar')
    org = get_organization(request)
    PipedreamCalendarConnection.objects.filter(organization=org).delete()
    messages.success(request, 'Google Calendar (Pipedream) disconnected.')
    return redirect('tickets:settings_google_calendar')
