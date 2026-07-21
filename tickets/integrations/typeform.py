"""Typeform integration views.

Moved out of tickets/views.py into the integrations package so the
integration is referenced rather than built into the core app.
Shared helpers remain in tickets.views and are imported below.
"""

from ..models import ExternalSurveyResponse
from ..models import ExternalSurveyUpload
from django.http import Http404
from django.http import HttpResponse
from django.http import HttpResponseBadRequest
from django.http import JsonResponse
from ..models import TypeformFormSubscription
from django.views.decorators.csrf import csrf_exempt
from django.core.cache import cache as django_cache
from django.utils import timezone as django_tz
from django.shortcuts import get_object_or_404
from ..utils import get_organization
import json
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import redirect
from django.shortcuts import render
from ..utils import require_admin
from django.views.decorators.http import require_http_methods
from ..utils import require_org
from django.conf import settings
from django.utils.http import url_has_allowed_host_and_scheme
from ..views import (
    _event_stats_cache_key,
    _invalidate_event_upload_stats_cache,
    _is_publicly_reachable,
    _refresh_subscription_questions,
    _typeform_webhook_url,
    logger,
)


@login_required
@require_org
@require_admin
def typeform_settings(request):
    """Show Typeform connection status, current form subscriptions, and sync controls."""
    org = get_organization(request)
    subscriptions = (
        TypeformFormSubscription.objects
        .filter(organization=org)
        .order_by('-is_active', '-created_at')
    )
    return render(request, 'tickets/typeform_settings.html', {
        'org': org,
        'is_connected': bool(org.typeform_access_token),
        'subscriptions': subscriptions,
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def typeform_connect(request):
    """Validate the posted Personal Access Token, save it, and stash the account email."""
    from ..services.typeform.client import TypeformAPIError, TypeformClient

    org = get_organization(request)
    token = (request.POST.get('access_token') or '').strip()
    if not token:
        messages.error(request, 'Paste a Typeform Personal Access Token.')
        return redirect('tickets:typeform_settings')

    try:
        me = TypeformClient(access_token=token).validate_token()
    except TypeformAPIError as exc:
        messages.error(request, f'Could not verify Typeform credentials: {exc}')
        return redirect('tickets:typeform_settings')

    org.typeform_access_token = token
    org.typeform_account_email = (me.get('email') or '')[:254]
    org.typeform_validated_at = django_tz.now()
    org.save(update_fields=[
        'typeform_access_token', 'typeform_account_email', 'typeform_validated_at',
    ])
    messages.success(request, 'Typeform connected.')
    return redirect('tickets:typeform_settings')


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def typeform_disconnect(request):
    """Disconnect Typeform: delete all webhooks and clear org credentials."""
    from ..services.typeform.client import TypeformAPIError, TypeformClient

    org = get_organization(request)
    if not org.typeform_access_token:
        messages.info(request, 'Typeform was not connected.')
        return redirect('tickets:typeform_settings')

    client = TypeformClient(access_token=org.typeform_access_token)
    tag = getattr(settings, 'TYPEFORM_WEBHOOK_TAG', 'cue')
    for sub in TypeformFormSubscription.objects.filter(organization=org, is_active=True):
        try:
            client.delete_webhook(sub.form_id, tag=tag)
        except TypeformAPIError as exc:
            logger.warning('Failed to delete Typeform webhook for sub %s: %s', sub.id, exc)
        sub.is_active = False
        sub.webhook_id = ''
        sub.last_sync_error = ''
        sub.save(update_fields=['is_active', 'webhook_id', 'last_sync_error'])

    org.typeform_access_token = ''
    org.typeform_account_email = ''
    org.typeform_validated_at = None
    org.save(update_fields=[
        'typeform_access_token', 'typeform_account_email', 'typeform_validated_at',
    ])
    messages.success(request, 'Typeform disconnected.')
    return redirect('tickets:typeform_settings')


@login_required
@require_org
@require_admin
def typeform_form_picker(request):
    """List Typeform forms; let the org check which to subscribe (creates webhooks)."""
    from ..services.typeform.client import TypeformAPIError, TypeformClient

    org = get_organization(request)
    if not org.typeform_access_token:
        messages.error(request, 'Connect Typeform first.')
        return redirect('tickets:typeform_settings')

    client = TypeformClient(access_token=org.typeform_access_token)
    try:
        forms = client.list_forms()
    except TypeformAPIError as exc:
        messages.error(request, f'Could not load Typeform forms: {exc}')
        return redirect('tickets:typeform_settings')

    existing = {s.form_id: s for s in TypeformFormSubscription.objects.filter(organization=org)}

    if request.method == 'POST':
        selected_form_ids = set(request.POST.getlist('form_ids'))
        tag = getattr(settings, 'TYPEFORM_WEBHOOK_TAG', 'cue')

        forms_by_id = {f.get('id'): f for f in forms if f.get('id')}
        created = 0
        for form_id in selected_form_ids:
            form_meta = forms_by_id.get(form_id)
            if not form_meta:
                continue
            sub = existing.get(form_id)
            if sub is None:
                upload = ExternalSurveyUpload.objects.create(
                    organization=org,
                    filename=f'Typeform: {form_meta.get("title") or form_id}',
                    status=ExternalSurveyUpload.Status.COMPLETED,
                )
                sub = TypeformFormSubscription.objects.create(
                    organization=org,
                    form_id=form_id,
                    form_title=(form_meta.get('title') or '')[:255],
                    upload=upload,
                )
            else:
                # Re-activating a previously deactivated subscription. Clear any stale
                # last_sync_error from a prior (possibly expired) token so the row
                # doesn't show a misleading red error line after reconnect.
                sub.is_active = True
                sub.form_title = (form_meta.get('title') or sub.form_title)[:255]
                sub.last_sync_error = ''
                sub.save(update_fields=['is_active', 'form_title', 'last_sync_error'])

            # Snapshot the form's questions so we can render them on Surveys pages
            # without re-fetching the definition on every read. Best-effort: a
            # Typeform hiccup here just means we keep the existing snapshot.
            try:
                definition = client.get_form(form_id)
                _refresh_subscription_questions(sub, definition)
            except TypeformAPIError as exc:
                logger.warning(
                    'Could not snapshot questions for sub %s: %s', sub.id, exc,
                )
                definition = None

            # Seed a starting field_map from the form definition so structured
            # columns (nps_score, overall_rating, text_feedback, …) populate on
            # first sync without the user having to visit the mapping editor.
            # Only when never configured before (None) — an explicit `{}` save
            # means "ignore everything" and must be preserved.
            if definition and sub.field_map is None:
                from ..services.typeform.field_mapping import auto_field_map
                suggested = auto_field_map(definition)
                if suggested:
                    sub.field_map = suggested
                    sub.save(update_fields=['field_map'])

            webhook_url = _typeform_webhook_url(request, sub.id)
            if not _is_publicly_reachable(webhook_url):
                messages.warning(
                    request,
                    f'Form "{sub.form_title}" subscribed without a webhook: {webhook_url} is not '
                    'publicly reachable. Use "Sync recent" to pull responses manually, or set '
                    'TYPEFORM_WEBHOOK_BASE_URL to a public tunnel (e.g. ngrok) and re-pick.',
                )
            else:
                try:
                    webhook = client.create_webhook(
                        form_id=form_id,
                        url=webhook_url,
                        secret=sub.webhook_secret,
                        tag=tag,
                    )
                    sub.webhook_id = str(webhook.get('id') or '')[:64]
                    sub.save(update_fields=['webhook_id'])
                except TypeformAPIError as exc:
                    messages.warning(
                        request,
                        f'Form "{sub.form_title}" subscribed but webhook registration failed: {exc}',
                    )
            created += 1

        # Deactivate any previously-active subscriptions that the org just unchecked.
        for sub in existing.values():
            if sub.is_active and sub.form_id not in selected_form_ids:
                try:
                    client.delete_webhook(sub.form_id, tag=tag)
                except TypeformAPIError:
                    pass
                sub.is_active = False
                sub.webhook_id = ''
                sub.save(update_fields=['is_active', 'webhook_id'])

        messages.success(request, f'Subscribed to {created} Typeform form(s).')
        return redirect('tickets:typeform_settings')

    rows = []
    for form in forms:
        sub = existing.get(form.get('id'))
        rows.append({
            'id': form.get('id'),
            'title': form.get('title') or form.get('id'),
            'last_updated_at': form.get('last_updated_at'),
            'is_subscribed': bool(sub and sub.is_active),
        })
    return render(request, 'tickets/typeform_form_picker.html', {
        'forms': rows,
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def typeform_form_sync(request, sub_id):
    """Trigger an on-demand pull of recent responses for one subscription."""
    from ..tasks import sync_typeform_form_task

    org = get_organization(request)
    subscription = get_object_or_404(
        TypeformFormSubscription.objects.filter(organization=org), id=sub_id,
    )
    sync_typeform_form_task.delay(str(subscription.id))
    messages.success(request, f'Syncing "{subscription.form_title}" — new responses will appear shortly.')

    next_url = (request.POST.get('next') or '').strip()
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    return redirect('tickets:typeform_settings')


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def typeform_form_unsubscribe(request, sub_id):
    """Delete the webhook and deactivate one form subscription."""
    from ..services.typeform.client import TypeformAPIError, TypeformClient

    org = get_organization(request)
    subscription = get_object_or_404(
        TypeformFormSubscription.objects.filter(organization=org), id=sub_id,
    )
    if org.typeform_access_token:
        tag = getattr(settings, 'TYPEFORM_WEBHOOK_TAG', 'cue')
        try:
            TypeformClient(access_token=org.typeform_access_token).delete_webhook(
                subscription.form_id, tag=tag,
            )
        except TypeformAPIError as exc:
            logger.warning('Failed to delete webhook for sub %s: %s', subscription.id, exc)
    subscription.is_active = False
    subscription.webhook_id = ''
    subscription.save(update_fields=['is_active', 'webhook_id'])
    messages.success(request, f'Unsubscribed from "{subscription.form_title}".')
    return redirect('tickets:typeform_settings')


@login_required
@require_org
@require_admin
def typeform_form_mapping(request, sub_id):
    """Per-form field mapping editor: pick which Typeform question feeds which
    ExternalSurveyResponse column. New ingests use the saved map; existing rows
    can be re-projected by ticking the backfill checkbox.
    """
    from collections import OrderedDict

    from ..services.typeform.client import TypeformAPIError, TypeformClient
    from ..services.typeform.field_mapping import (
        TARGET_FIELDS, apply_field_map, auto_field_map, flatten_form_fields,
    )

    SAMPLE_RESPONSE_LIMIT = 50
    SAMPLE_VALUES_PER_FIELD = 3

    org = get_organization(request)
    subscription = get_object_or_404(
        TypeformFormSubscription.objects.filter(organization=org), id=sub_id,
    )

    if not org.typeform_access_token:
        messages.error(request, 'Connect Typeform first to edit field mappings.')
        return redirect('tickets:typeform_settings')

    client = TypeformClient(access_token=org.typeform_access_token)
    try:
        definition = client.get_form(subscription.form_id)
    except TypeformAPIError as exc:
        messages.error(request, f'Could not load form definition: {exc}')
        definition = {'fields': []}
    else:
        # Refresh the persisted question snapshot used to display titles on the
        # Surveys tab without re-fetching the definition on every render.
        _refresh_subscription_questions(subscription, definition)

    flat_fields = flatten_form_fields(definition)

    if request.method == 'POST':
        new_map: dict = {}
        for field in flat_fields:
            key = field.get('ref') or field.get('id')
            if not key:
                continue
            value = (request.POST.get(f'map_{key}') or '').strip()
            if value and value in TARGET_FIELDS:
                new_map[key] = value
        subscription.field_map = new_map
        subscription.save(update_fields=['field_map'])

        backfilled = 0
        if request.POST.get('backfill') == '1' and subscription.upload_id:
            from ..services.typeform.ingest import apply_field_map_to_subscription
            backfilled, affected_event_ids = apply_field_map_to_subscription(
                subscription, new_map, limit=500,
            )
            for eid in affected_event_ids:
                django_cache.delete(_event_stats_cache_key(eid))
                _invalidate_event_upload_stats_cache(eid)

        msg = f'Field mapping saved ({len(new_map)} field(s)).'
        if backfilled:
            msg += f' Re-applied to {backfilled} existing response(s).'
        messages.success(request, msg)
        return redirect('tickets:typeform_form_mapping', sub_id=subscription.id)

    # Collect up to 3 distinct sample values per question, scanning the most
    # recent 50 ingested responses for this form. Lets the user map deterministically.
    samples_by_key: dict[str, list[str]] = {}
    if subscription.upload_id:
        recent_raws = (
            ExternalSurveyResponse.objects
            .filter(organization=org, upload_id=subscription.upload_id)
            .exclude(typeform_response_id='')
            .order_by('-responded_at')
            .values_list('raw_answers', flat=True)[:SAMPLE_RESPONSE_LIMIT]
        )
        seen: dict[str, OrderedDict] = {}
        for raw in recent_raws:
            for ans in raw or []:
                if not isinstance(ans, dict):
                    continue
                key = ans.get('ref') or ans.get('id')
                if not key:
                    continue
                value = ans.get('value')
                if value in (None, '', []):
                    continue
                text = (
                    ', '.join(str(v) for v in value)
                    if isinstance(value, list) else str(value)
                ).strip()
                if not text:
                    continue
                bucket = seen.setdefault(key, OrderedDict())
                if text not in bucket and len(bucket) < SAMPLE_VALUES_PER_FIELD:
                    bucket[text] = None
        samples_by_key = {k: list(v.keys()) for k, v in seen.items()}

    # `field_map is None` means the subscription has never been saved through this
    # editor — only then do we pre-fill the dropdowns with auto-detect suggestions.
    # An empty `{}` is a deliberate "everything Ignored" save and must be respected
    # on reload (no auto-detect bleed-through).
    if subscription.field_map is None:
        current_map = auto_field_map(definition)
    else:
        current_map = subscription.field_map
    fields = []
    for field in flat_fields:
        key = field.get('ref') or field.get('id')
        if not key:
            continue
        fields.append({
            'key': key,
            'title': field.get('title') or '(untitled)',
            'group_title': field.get('group_title') or '',
            'type': field.get('type'),
            'mapped_to': current_map.get(key, ''),
            'samples': samples_by_key.get(key, []),
        })

    return render(request, 'tickets/typeform_form_mapping.html', {
        'subscription': subscription,
        'fields': fields,
        'target_fields': TARGET_FIELDS,
        'has_saved_mapping': subscription.field_map is not None,
    })


@csrf_exempt
@require_http_methods(["POST"])
def typeform_webhook(request, sub_id):
    """Receive Typeform webhook deliveries: verify HMAC, ingest one response, queue LLM match."""
    import base64
    import hashlib
    import hmac as hmac_mod

    from ..services.typeform.ingest import ingest_response
    from ..tasks import match_survey_response_to_event_task

    try:
        subscription = TypeformFormSubscription.objects.select_related('organization').get(
            id=sub_id, is_active=True,
        )
    except (TypeformFormSubscription.DoesNotExist, ValueError, Http404):
        return HttpResponse(status=404)

    signature_header = request.headers.get('Typeform-Signature', '')
    if not signature_header.startswith('sha256='):
        return HttpResponse('Invalid signature header', status=401)
    posted_b64 = signature_header[len('sha256='):]

    raw_body = request.body
    digest = hmac_mod.new(
        subscription.webhook_secret.encode('utf-8'),
        raw_body, hashlib.sha256,
    ).digest()
    expected_b64 = base64.b64encode(digest).decode('ascii')
    if not hmac_mod.compare_digest(posted_b64, expected_b64):
        return HttpResponse('Bad signature', status=401)

    try:
        payload = json.loads(raw_body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return HttpResponseBadRequest('Invalid JSON')

    form_response = payload.get('form_response')
    if not form_response:
        return HttpResponseBadRequest('Missing form_response')

    response, created = ingest_response(subscription, form_response)
    if response and created:
        match_survey_response_to_event_task.delay(str(response.id))

    return JsonResponse({'ok': True, 'created': bool(created)})
