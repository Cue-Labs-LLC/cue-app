"""SlickText integration views.

Moved out of tickets/views.py into the integrations package so the
integration is referenced rather than built into the core app.
Shared helpers remain in tickets.views and are imported below.
"""

from ..models import Event
from ..models import EventSMSCampaign
from decimal import InvalidOperation
from django.http import JsonResponse
from ..services.slicktext import SlickTextAPIError
from ..services.slicktext_campaign_matcher import SlickTextCampaignMatcher
from ..services.slicktext import SlickTextClient
from django.db import connection
from django.utils import timezone as django_tz
from django.shortcuts import get_object_or_404
from ..utils import get_organization
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import redirect
from django.shortcuts import render
from ..utils import require_admin
from django.views.decorators.http import require_http_methods
from ..utils import require_org
from ..views import (
    _format_meta_ads_datetime,
    _get_active_slicktext_campaign_or_404,
    _get_slicktext_connection,
    _invalidate_marketing_cache,
    _marketing_tab_redirect,
    _parse_optional_decimal,
    _parse_optional_int,
    _recompute_utm_attribution_for_event,
    _save_slicktext_campaign_from_report,
    _serialize_slicktext_campaign,
    _serialize_sms_row,
    _slicktext_fetch_campaign_with_analytics,
    _wants_marketing_json,
    logger,
)


@login_required
@require_org
@require_admin
def slicktext_settings(request):
    """Show SlickText connection status and the credential form."""
    org = get_organization(request)
    return render(request, 'tickets/settings_slicktext.html', {
        'org': org,
        'slicktext_connection': _get_slicktext_connection(org),
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def slicktext_save(request):
    """Validate posted SlickText API key, fetch the brand, and persist credentials."""
    org = get_organization(request)
    api_key = (request.POST.get('api_key') or '').strip()
    if not api_key:
        messages.error(request, 'Enter your SlickText API key.')
        return redirect('tickets:slicktext_settings')

    try:
        brand = SlickTextClient(api_key).get_brand()
    except SlickTextAPIError as exc:
        messages.error(request, f'Could not verify SlickText credentials: {exc}')
        return redirect('tickets:slicktext_settings')

    brand_id = str(brand.get('brand_id') or brand.get('id') or '')
    if not brand_id:
        messages.error(request, 'SlickText did not return a brand ID for this API key.')
        return redirect('tickets:slicktext_settings')

    org.slicktext_api_key = api_key
    org.slicktext_brand_id = brand_id
    org.slicktext_brand_name = str(brand.get('name') or brand.get('legal_name') or '')
    org.slicktext_validated_at = django_tz.now()
    org.save(update_fields=[
        'slicktext_api_key',
        'slicktext_brand_id',
        'slicktext_brand_name',
        'slicktext_validated_at',
    ])
    messages.success(request, 'SlickText connected.')
    return redirect('tickets:slicktext_settings')


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def slicktext_disconnect(request):
    """Disconnect SlickText from the current org."""
    org = get_organization(request)
    if _get_slicktext_connection(org):
        org.slicktext_api_key = ''
        org.slicktext_brand_id = ''
        org.slicktext_brand_name = ''
        org.slicktext_validated_at = None
        org.save(update_fields=[
            'slicktext_api_key',
            'slicktext_brand_id',
            'slicktext_brand_name',
            'slicktext_validated_at',
        ])
        messages.success(request, 'SlickText disconnected.')
    else:
        messages.info(request, 'SlickText was not connected.')
    return redirect('tickets:slicktext_settings')


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_slicktext_metrics_edit(request, event_id, sms_campaign_id):
    """Partial update of manual_clicks/manual_orders/manual_revenue."""
    org = get_organization(request)
    event, sms_campaign = _get_active_slicktext_campaign_or_404(org, event_id, sms_campaign_id)
    ajax = _wants_marketing_json(request)
    update_fields = []
    int_fields = ['manual_audience', 'manual_clicks', 'manual_unsubscribes', 'manual_orders']
    try:
        for field in int_fields:
            if field in request.POST:
                setattr(sms_campaign, field, _parse_optional_int(request.POST.get(field)))
                update_fields.append(field)
        if 'manual_revenue' in request.POST:
            sms_campaign.manual_revenue = _parse_optional_decimal(request.POST.get('manual_revenue'))
            update_fields.append('manual_revenue')
    except (ValueError, InvalidOperation):
        msg = 'Enter non-negative numbers (or leave a field blank to use the API value).'
        if ajax:
            return JsonResponse({'ok': False, 'error': msg}, status=400)
        messages.error(request, msg)
        return _marketing_tab_redirect(event)
    if not update_fields:
        if ajax:
            return JsonResponse({'ok': True, 'row': _serialize_sms_row(sms_campaign)})
        return _marketing_tab_redirect(event)
    sms_campaign.updated_by = request.user
    update_fields += ['updated_by', 'updated_at']
    sms_campaign.save(update_fields=update_fields)
    _invalidate_marketing_cache(org)
    if ajax:
        return JsonResponse({'ok': True, 'row': _serialize_sms_row(sms_campaign)})
    messages.success(request, f'Updated metrics for "{sms_campaign.name}".')
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def event_slicktext_match(request, event_id):
    """Rank SlickText broadcasts that likely correspond to this event."""
    org = get_organization(request)
    wants_json = request.GET.get('format') == 'json'
    event = get_object_or_404(
        Event.objects.filter(organization=org).select_related('venue'),
        id=event_id,
    )
    connection = _get_slicktext_connection(org)
    if not connection:
        if wants_json:
            return JsonResponse({
                'success': False,
                'error': 'Connect SlickText before matching campaigns.',
            }, status=400)
        messages.error(request, 'Connect SlickText before matching campaigns.')
        return redirect('tickets:slicktext_settings')

    try:
        client = SlickTextClient(connection.slicktext_api_key, connection.slicktext_brand_id)
        campaigns = client.list_campaigns()
        match_result = SlickTextCampaignMatcher(org).rank(event, campaigns)
    except SlickTextAPIError as exc:
        if wants_json:
            return JsonResponse({'success': False, 'error': f'Could not load SlickText campaigns: {exc}'}, status=502)
        messages.error(request, f'Could not load SlickText campaigns: {exc}')
        return redirect('tickets:event_detail', event_id=event.id)
    except Exception as exc:
        logger.exception("SlickText campaign matching failed for event %s: %s", event.id, exc)
        if wants_json:
            return JsonResponse({
                'success': False,
                'error': 'Could not rank SlickText campaigns. Please check your OpenAI configuration and try again.',
            }, status=500)
        messages.error(request, 'Could not rank SlickText campaigns. Please check your OpenAI configuration and try again.')
        return redirect('tickets:event_detail', event_id=event.id)

    linked_ids = set(
        EventSMSCampaign.objects.filter(
            event=event,
            source='slicktext',
            deleted_at__isnull=True,
        ).exclude(external_id='').values_list('external_id', flat=True)
    )

    campaigns_by_id = {str(campaign.get('campaign_id') or campaign.get('id')): campaign for campaign in campaigns}
    candidates = []
    for candidate in match_result.candidates:
        campaign = campaigns_by_id.get(candidate.campaign_id)
        if not campaign:
            continue
        confidence_pct = int(round(candidate.confidence * 100))
        if candidate.confidence >= 0.7:
            confidence_class = 'bg-success'
        elif candidate.confidence >= 0.3:
            confidence_class = 'bg-warning'
        else:
            confidence_class = 'bg-secondary'
        external_id = str(campaign.get('campaign_id') or campaign.get('id'))
        candidates.append({
            'campaign': campaign,
            'confidence': candidate.confidence,
            'confidence_pct': confidence_pct,
            'confidence_class': confidence_class,
            'reasoning': candidate.reasoning,
            'is_linked': external_id in linked_ids,
        })

    if wants_json:
        return JsonResponse({
            'success': True,
            'brand_name': connection.slicktext_brand_name or connection.slicktext_brand_id,
            'candidates': [
                {
                    'campaign_id': str(item['campaign'].get('campaign_id') or item['campaign'].get('id')),
                    'name': item['campaign'].get('name') or '',
                    'message': item['campaign'].get('body') or item['campaign'].get('message') or '',
                    'send_time': _format_meta_ads_datetime(
                        item['campaign'].get('finished')
                        or item['campaign'].get('started')
                        or item['campaign'].get('scheduled')
                    ) or 'Unknown',
                    'audience_size': item['campaign'].get('audience_size') or 0,
                    'status': item['campaign'].get('status') or '',
                    'confidence': item['confidence'],
                    'confidence_pct': item['confidence_pct'],
                    'confidence_class': item['confidence_class'],
                    'reasoning': item['reasoning'],
                    'is_linked': item['is_linked'],
                }
                for item in candidates
            ],
        })

    return render(request, 'tickets/event_slicktext_match.html', {
        'event': event,
        'candidates': candidates,
        'brand_name': connection.slicktext_brand_name or connection.slicktext_brand_id,
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_slicktext_apply(request, event_id):
    """Pull SlickText campaign + analytics for each chosen ID and upsert as EventSMSCampaign rows."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    wants_json = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    connection = _get_slicktext_connection(org)
    if not connection:
        if wants_json:
            return JsonResponse({'success': False, 'error': 'Connect SlickText before applying campaign results.'}, status=400)
        messages.error(request, 'Connect SlickText before applying campaign results.')
        return redirect('tickets:slicktext_settings')

    campaign_ids = [cid.strip() for cid in request.POST.getlist('campaign_id') if cid.strip()]
    if not campaign_ids:
        if wants_json:
            return JsonResponse({'success': False, 'error': 'Choose at least one SlickText broadcast to apply.'}, status=400)
        messages.error(request, 'Choose at least one SlickText broadcast to apply.')
        return redirect('tickets:event_slicktext_match', event_id=event.id)

    confidences = request.POST.getlist('confidence')
    reasonings = request.POST.getlist('reasoning')

    client = SlickTextClient(connection.slicktext_api_key, connection.slicktext_brand_id)
    added_titles = []
    updated_titles = []
    failed_ids = []
    for index, campaign_id in enumerate(campaign_ids):
        confidence = confidences[index] if index < len(confidences) else None
        reasoning = (reasonings[index] if index < len(reasonings) else '').strip()
        try:
            report = _slicktext_fetch_campaign_with_analytics(client, campaign_id)
            sms_campaign, created = _save_slicktext_campaign_from_report(
                event,
                report,
                user=request.user,
                match_confidence=confidence or None,
                match_reasoning=reasoning,
            )
        except SlickTextAPIError as exc:
            logger.warning(
                "SlickText apply failed for org=%s event=%s campaign=%s: %s",
                org.id, event.id, campaign_id, exc,
            )
            failed_ids.append(campaign_id)
            continue
        if created:
            added_titles.append(sms_campaign.name)
        else:
            updated_titles.append(sms_campaign.name)

    succeeded = len(added_titles) + len(updated_titles)
    if succeeded:
        # Newly linked broadcasts have fresh external_ids to match orders against.
        _recompute_utm_attribution_for_event(org, event)
    if succeeded == 1:
        only_title = (added_titles + updated_titles)[0]
        verb = 'added' if added_titles else 'updated'
        success_msg = f'SlickText broadcast "{only_title}" {verb} for this event.'
    elif succeeded:
        parts = []
        if added_titles:
            parts.append(f'added {len(added_titles)}')
        if updated_titles:
            parts.append(f'updated {len(updated_titles)}')
        success_msg = f'Linked {succeeded} SlickText broadcasts to this event ({", ".join(parts)}).'
    else:
        success_msg = ''

    if wants_json:
        all_linked = list(
            EventSMSCampaign.objects.filter(
                event=event,
                source='slicktext',
                deleted_at__isnull=True,
            ).order_by('-send_time', '-created_at')
        )
        return JsonResponse({
            'success': succeeded > 0,
            'message': success_msg,
            'added_count': len(added_titles),
            'updated_count': len(updated_titles),
            'failed_ids': failed_ids,
            'campaigns': [_serialize_slicktext_campaign(item, event) for item in all_linked],
        })

    if succeeded:
        messages.success(request, success_msg)

    if failed_ids:
        messages.error(
            request,
            'Could not pull results from SlickText for: ' + ', '.join(failed_ids),
        )

    if not succeeded and not failed_ids:
        messages.error(request, 'No SlickText broadcasts were applied.')

    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_slicktext_refresh_all(request, event_id):
    """Refresh all linked SlickText campaign analytics for an event."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    connection = _get_slicktext_connection(org)
    if not connection:
        return JsonResponse({
            'success': False,
            'error': 'Connect SlickText before refreshing campaign results.',
        }, status=400)

    sms_campaigns = list(
        EventSMSCampaign.objects.filter(
            event=event,
            source='slicktext',
            deleted_at__isnull=True,
        ).exclude(external_id='')
    )
    client = SlickTextClient(connection.slicktext_api_key, connection.slicktext_brand_id)
    had_error = False
    refreshed = []

    for sms_campaign in sms_campaigns:
        try:
            report = _slicktext_fetch_campaign_with_analytics(client, sms_campaign.external_id)
            refreshed_row, _created = _save_slicktext_campaign_from_report(event, report, user=request.user)
            refreshed.append(refreshed_row)
        except SlickTextAPIError as exc:
            had_error = True
            logger.warning(
                "SlickText bulk refresh failed for org=%s event=%s sms_campaign=%s campaign=%s: %s",
                org.id, event.id, sms_campaign.id, sms_campaign.external_id, exc,
            )
            refreshed.append(sms_campaign)

    refreshed.sort(
        key=lambda item: (item.send_time or django_tz.datetime.min.replace(tzinfo=django_tz.utc), item.created_at),
        reverse=True,
    )
    return JsonResponse({
        'success': True,
        'had_error': had_error,
        'campaigns': [_serialize_slicktext_campaign(item, event) for item in refreshed],
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_slicktext_refresh(request, event_id, sms_campaign_id):
    """Refresh a single linked SlickText campaign."""
    org = get_organization(request)
    event, sms_campaign = _get_active_slicktext_campaign_or_404(org, event_id, sms_campaign_id)
    ajax = _wants_marketing_json(request)
    connection = _get_slicktext_connection(org)
    if not connection:
        msg = 'Connect SlickText before refreshing campaign results.'
        if ajax:
            return JsonResponse({'ok': False, 'error': msg}, status=400)
        messages.error(request, msg)
        return redirect('tickets:slicktext_settings')

    try:
        client = SlickTextClient(connection.slicktext_api_key, connection.slicktext_brand_id)
        report = _slicktext_fetch_campaign_with_analytics(client, sms_campaign.external_id)
        refreshed, _created = _save_slicktext_campaign_from_report(event, report, user=request.user)
    except SlickTextAPIError as exc:
        logger.warning(
            "SlickText row refresh failed for org=%s event=%s sms_campaign=%s campaign=%s: %s",
            org.id, event.id, sms_campaign.id, sms_campaign.external_id, exc,
        )
        msg = 'Could not refresh this SlickText broadcast.'
        if ajax:
            return JsonResponse({'ok': False, 'error': msg}, status=502)
        messages.warning(request, msg)
        return _marketing_tab_redirect(event)

    if ajax:
        return JsonResponse({'ok': True, 'row': _serialize_sms_row(refreshed)})
    messages.success(request, f'SlickText broadcast "{refreshed.name}" refreshed.')
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_slicktext_remove(request, event_id, sms_campaign_id):
    """Unlink a SlickText broadcast from an event by soft-deleting its stored results."""
    org = get_organization(request)
    event, sms_campaign = _get_active_slicktext_campaign_or_404(org, event_id, sms_campaign_id)

    sms_campaign.updated_by = request.user
    sms_campaign.save(update_fields=['updated_by'])
    sms_campaign.delete()

    if _wants_marketing_json(request):
        return JsonResponse({'ok': True, 'removed_id': str(sms_campaign.id)})
    messages.success(request, 'SlickText broadcast removed from this event.')
    return _marketing_tab_redirect(event)
