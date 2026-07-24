"""Self-serve webhook management views.

Org admins manage their outbound `WebhookEndpoint`s here (create / edit / delete /
rotate secret / send test), and browse the `WebhookDelivery` log. Pure UI over the
webhook backend in `tickets/services/webhooks/` + `deliver_webhook_task`; no model
changes. Mirrors the integration-view conventions in this package.
"""

import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from ..forms import WebhookEndpointForm
from ..models import WebhookEndpoint, WebhookDelivery, _generate_webhook_secret
from ..utils import get_organization, require_admin, require_org

DELIVERY_PAGE_SIZE = 25


@login_required
@require_org
@require_admin
def webhook_endpoint_list(request):
    org = get_organization(request)
    endpoints = WebhookEndpoint.objects.filter(organization=org)
    return render(request, 'tickets/webhook_endpoint_list.html', {
        'endpoints': endpoints,
    })


@login_required
@require_org
@require_admin
def webhook_endpoint_create(request):
    org = get_organization(request)
    if request.method == 'POST':
        form = WebhookEndpointForm(request.POST, organization=org)
        if form.is_valid():
            endpoint = form.save(commit=False)
            endpoint.organization = org
            endpoint.save()
            messages.success(
                request,
                f'Webhook endpoint "{endpoint.label}" created. Copy the signing secret below '
                'to verify deliveries on your receiver.',
            )
            return redirect('tickets:webhook_endpoint_edit', endpoint_id=endpoint.id)
    else:
        form = WebhookEndpointForm(organization=org)
    return render(request, 'tickets/webhook_endpoint_form.html', {
        'form': form,
        'action': 'Create',
    })


@login_required
@require_org
@require_admin
def webhook_endpoint_edit(request, endpoint_id):
    org = get_organization(request)
    endpoint = get_object_or_404(WebhookEndpoint.objects.filter(organization=org), id=endpoint_id)
    if request.method == 'POST':
        form = WebhookEndpointForm(request.POST, instance=endpoint, organization=org)
        if form.is_valid():
            form.save()
            messages.success(request, 'Webhook endpoint updated.')
            return redirect('tickets:webhook_endpoint_edit', endpoint_id=endpoint.id)
    else:
        form = WebhookEndpointForm(instance=endpoint, organization=org)
    return render(request, 'tickets/webhook_endpoint_form.html', {
        'form': form,
        'action': 'Edit',
        'endpoint': endpoint,
    })


@login_required
@require_org
@require_admin
def webhook_endpoint_delete(request, endpoint_id):
    org = get_organization(request)
    endpoint = get_object_or_404(WebhookEndpoint.objects.filter(organization=org), id=endpoint_id)
    if request.method == 'POST':
        label = endpoint.label
        endpoint.delete()
        messages.success(request, f'Webhook endpoint "{label}" deleted.')
        return redirect('tickets:webhook_endpoint_list')
    return render(request, 'tickets/webhook_endpoint_delete.html', {
        'endpoint': endpoint,
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def webhook_endpoint_rotate_secret(request, endpoint_id):
    org = get_organization(request)
    endpoint = get_object_or_404(WebhookEndpoint.objects.filter(organization=org), id=endpoint_id)
    endpoint.secret = _generate_webhook_secret()
    endpoint.save(update_fields=['secret', 'updated_at'])
    messages.success(
        request,
        'Signing secret rotated. Update your receiver with the new secret — deliveries now sign with it.',
    )
    return redirect('tickets:webhook_endpoint_edit', endpoint_id=endpoint.id)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def webhook_endpoint_test(request, endpoint_id):
    org = get_organization(request)
    endpoint = get_object_or_404(WebhookEndpoint.objects.filter(organization=org), id=endpoint_id)
    if not endpoint.is_active:
        messages.warning(request, 'Activate the endpoint before sending a test delivery.')
        return redirect('tickets:webhook_endpoint_edit', endpoint_id=endpoint.id)

    from ..tasks import deliver_webhook_task
    from ..services.webhooks.constants import WEBHOOK_EVENT_TYPES

    subscribed = [t for t in (endpoint.event_types or []) if t in WEBHOOK_EVENT_TYPES]
    event_type = subscribed[0] if subscribed else WEBHOOK_EVENT_TYPES[0]
    payload = _sample_payload(org, event_type)
    deliver_webhook_task.delay(str(endpoint.id), event_type, str(uuid.uuid4()), payload)
    messages.success(request, f'Test "{event_type}" delivery sent. Check the delivery log for the result.')
    return redirect(f"{_delivery_list_url()}?endpoint={endpoint.id}")


@login_required
@require_org
@require_admin
def webhook_delivery_list(request):
    org = get_organization(request)
    deliveries = (
        WebhookDelivery.objects.filter(organization=org)
        .select_related('endpoint')
        .order_by('-created_at')
    )
    endpoint_id = request.GET.get('endpoint')
    selected_endpoint = None
    if endpoint_id:
        selected_endpoint = WebhookEndpoint.objects.filter(organization=org, id=endpoint_id).first()
        if selected_endpoint:
            deliveries = deliveries.filter(endpoint=selected_endpoint)
    page = Paginator(deliveries, DELIVERY_PAGE_SIZE).get_page(request.GET.get('page'))
    return render(request, 'tickets/webhook_delivery_list.html', {
        'page_obj': page,
        'deliveries': page.object_list,
        'endpoints': WebhookEndpoint.objects.filter(organization=org),
        'selected_endpoint': selected_endpoint,
    })


@login_required
@require_org
@require_admin
def webhook_delivery_detail(request, delivery_id):
    org = get_organization(request)
    delivery = get_object_or_404(
        WebhookDelivery.objects.filter(organization=org).select_related('endpoint'),
        id=delivery_id,
    )
    import json
    return render(request, 'tickets/webhook_delivery_detail.html', {
        'delivery': delivery,
        'payload_json': json.dumps(delivery.payload, indent=2, sort_keys=True),
    })


# --- helpers ---------------------------------------------------------------

def _delivery_list_url():
    from django.urls import reverse
    return reverse('tickets:webhook_delivery_list')


def _sample_payload(org, event_type):
    """Build a representative payload for a test delivery.

    Uses the org's most recent matching object via the real payload builders so
    the test looks like production; falls back to a synthetic stub if the org has
    no such object yet. Tagged with `test: True` so receivers can ignore it.
    """
    from ..models import Event, TicketOrder, Customer
    from ..services.webhooks import (
        EVENT_CREATED, ORDER_CREATED, CUSTOMER_CREATED,
        build_event_payload, build_order_payload, build_customer_payload,
    )

    payload = None
    if event_type == EVENT_CREATED:
        obj = Event.objects.filter(organization=org).select_related('venue').order_by('-created_at').first()
        if obj:
            payload = build_event_payload(obj)
    elif event_type == ORDER_CREATED:
        obj = (TicketOrder.objects.filter(event__organization=org)
               .select_related('event', 'customer').order_by('-created_at').first())
        if obj:
            payload = build_order_payload(obj)
    elif event_type == CUSTOMER_CREATED:
        obj = Customer.objects.filter(organization=org).order_by('-created_at').first()
        if obj:
            payload = build_customer_payload(obj)

    if payload is None:
        payload = {'id': str(uuid.uuid4()), 'sample': True}
    payload['test'] = True
    return payload
