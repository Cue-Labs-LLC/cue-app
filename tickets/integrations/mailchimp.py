"""Mailchimp integration views.

Moved out of tickets/views.py into the integrations package so the
integration is referenced rather than built into the core app.
Shared helpers remain in tickets.views and are imported below.
"""

from ..models import Event
from ..models import EventEmailCampaign
from decimal import InvalidOperation
from django.http import JsonResponse
from ..services.mailchimp import MailchimpAPIError
from ..services.mailchimp_campaign_matcher import MailchimpCampaignMatcher
from ..services.mailchimp import MailchimpClient
from ..services.mailchimp import build_authorize_url
from django.db import connection
from django.utils import timezone as django_tz
from ..services.mailchimp import exchange_code_for_token
from ..services.mailchimp import fetch_org_reports_cached
from ..services.mailchimp import get_oauth_metadata
from django.shortcuts import get_object_or_404
from ..utils import get_organization
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import redirect
from django.shortcuts import render
from ..utils import require_admin
from django.views.decorators.http import require_http_methods
from ..utils import require_org
from django.urls import reverse
import secrets
from django.conf import settings
from ..views import (
    _format_meta_ads_datetime,
    _get_active_mailchimp_campaign_or_404,
    _get_mailchimp_connection,
    _invalidate_marketing_cache,
    _marketing_tab_redirect,
    _parse_optional_decimal,
    _parse_optional_int,
    _save_mailchimp_campaign_from_report,
    _serialize_email_row,
    _serialize_mailchimp_campaign,
    _wants_marketing_json,
    logger,
)


@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def mailchimp_settings(request):
    """Show Mailchimp connection state for the current org."""
    org = get_organization(request)
    return render(request, 'tickets/settings_mailchimp.html', {
        'org': org,
        'mailchimp_connection': _get_mailchimp_connection(org),
        'mailchimp_configured': bool(settings.MAILCHIMP_CLIENT_ID and settings.MAILCHIMP_CLIENT_SECRET),
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def mailchimp_connect(request):
    """Start direct Mailchimp OAuth for report access."""
    org = get_organization(request)
    if not settings.MAILCHIMP_CLIENT_ID or not settings.MAILCHIMP_CLIENT_SECRET:
        messages.error(request, 'Mailchimp OAuth is not configured. Add MAILCHIMP_CLIENT_ID and MAILCHIMP_CLIENT_SECRET.')
        return redirect('tickets:mailchimp_settings')

    callback_url = request.build_absolute_uri(reverse('tickets:mailchimp_callback'))
    state = secrets.token_urlsafe(32)
    request.session['mailchimp_oauth_state'] = state
    return redirect(build_authorize_url(callback_url, state))


@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def mailchimp_callback(request):
    """Persist direct Mailchimp OAuth credentials after authorization."""
    org = get_organization(request)
    if request.GET.get('error'):
        messages.error(request, request.GET.get('error_description') or request.GET.get('error') or 'Mailchimp authorization was cancelled.')
        return redirect('tickets:mailchimp_settings')

    expected_state = request.session.get('mailchimp_oauth_state')
    state = request.GET.get('state')
    if not expected_state or not state or state != expected_state:
        messages.error(request, 'Mailchimp authorization could not be verified. Please try connecting again.')
        return redirect('tickets:mailchimp_settings')
    request.session.pop('mailchimp_oauth_state', None)

    code = request.GET.get('code')
    if not code:
        messages.error(request, 'Mailchimp did not return an authorization code. Please try connecting again.')
        return redirect('tickets:mailchimp_settings')

    callback_url = request.build_absolute_uri(reverse('tickets:mailchimp_callback'))
    try:
        access_token = exchange_code_for_token(code, callback_url)
        metadata = get_oauth_metadata(access_token)
        dc = metadata.get('dc')
        if not dc:
            raise MailchimpAPIError('Mailchimp did not return an account data center.')
        account_root = MailchimpClient(access_token, dc).get_account_root()
    except MailchimpAPIError as exc:
        messages.error(request, f'Could not verify Mailchimp connection: {exc}')
        return redirect('tickets:mailchimp_settings')

    login = metadata.get('login') or {}
    org.mailchimp_access_token = access_token
    org.mailchimp_dc = dc
    org.mailchimp_account_id = str(
        account_root.get('account_id')
        or metadata.get('account_id')
        or metadata.get('user_id')
        or ''
    )
    org.mailchimp_account_name = str(
        account_root.get('account_name')
        or metadata.get('accountname')
        or metadata.get('account_name')
        or ''
    )
    org.mailchimp_login_email = str(login.get('email') or metadata.get('login_email') or account_root.get('email') or '')
    org.save(update_fields=[
        'mailchimp_access_token',
        'mailchimp_dc',
        'mailchimp_account_id',
        'mailchimp_account_name',
        'mailchimp_login_email',
    ])

    messages.success(request, 'Mailchimp connected.')
    return redirect('tickets:mailchimp_settings')


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def mailchimp_disconnect(request):
    """Disconnect Mailchimp from the current org."""
    org = get_organization(request)
    if _get_mailchimp_connection(org):
        org.mailchimp_access_token = ''
        org.mailchimp_dc = ''
        org.mailchimp_account_id = ''
        org.mailchimp_account_name = ''
        org.mailchimp_login_email = ''
        org.save(update_fields=[
            'mailchimp_access_token',
            'mailchimp_dc',
            'mailchimp_account_id',
            'mailchimp_account_name',
            'mailchimp_login_email',
        ])
        messages.success(request, 'Mailchimp disconnected.')
    else:
        messages.info(request, 'Mailchimp was not connected.')
    return redirect('tickets:mailchimp_settings')


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def mailchimp_save_hints(request):
    """Save the org's free-form campaign naming hints for the AI matcher."""
    org = get_organization(request)
    hints = request.POST.get('campaign_title_hints', '').strip()
    if len(hints) > 2000:
        messages.error(request, 'Campaign naming hints must be 2000 characters or fewer.')
        return redirect('tickets:mailchimp_settings')
    org.mailchimp_campaign_title_hints = hints
    org.save(update_fields=['mailchimp_campaign_title_hints'])
    messages.success(request, 'Campaign naming hints saved.')
    return redirect('tickets:mailchimp_settings')


@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def event_mailchimp_match(request, event_id):
    """Rank Mailchimp campaigns that likely correspond to this event."""
    org = get_organization(request)
    wants_json = request.GET.get('format') == 'json'
    event = get_object_or_404(
        Event.objects.filter(organization=org).select_related('venue'),
        id=event_id,
    )
    connection = _get_mailchimp_connection(org)
    if not connection:
        if wants_json:
            return JsonResponse({
                'success': False,
                'error': 'Connect Mailchimp before matching campaigns.',
            }, status=400)
        messages.error(request, 'Connect Mailchimp before matching campaigns.')
        return redirect('tickets:mailchimp_settings')

    try:
        reports = fetch_org_reports_cached(org)
        match_result = MailchimpCampaignMatcher(org).rank(event, reports)
    except MailchimpAPIError as exc:
        if wants_json:
            return JsonResponse({'success': False, 'error': f'Could not load Mailchimp campaigns: {exc}'}, status=502)
        messages.error(request, f'Could not load Mailchimp campaigns: {exc}')
        return redirect('tickets:event_detail', event_id=event.id)
    except Exception as exc:
        logger.exception("Mailchimp campaign matching failed for event %s: %s", event.id, exc)
        if wants_json:
            return JsonResponse({
                'success': False,
                'error': 'Could not rank Mailchimp campaigns. Please check your OpenAI configuration and try again.',
            }, status=500)
        messages.error(request, 'Could not rank Mailchimp campaigns. Please check your OpenAI configuration and try again.')
        return redirect('tickets:event_detail', event_id=event.id)

    linked_ids = set(
        EventEmailCampaign.objects.filter(
            event=event,
            source='mailchimp',
            deleted_at__isnull=True,
        ).exclude(external_id='').values_list('external_id', flat=True)
    )

    reports_by_id = {str(report.get('id')): report for report in reports}
    candidates = []
    for candidate in match_result.candidates:
        report = reports_by_id.get(candidate.campaign_id)
        if not report:
            continue
        confidence_pct = int(round(candidate.confidence * 100))
        if candidate.confidence >= 0.7:
            confidence_class = 'bg-success'
        elif candidate.confidence >= 0.3:
            confidence_class = 'bg-warning'
        else:
            confidence_class = 'bg-secondary'
        candidates.append({
            'report': report,
            'confidence': candidate.confidence,
            'confidence_pct': confidence_pct,
            'confidence_class': confidence_class,
            'reasoning': candidate.reasoning,
            'is_linked': str(report.get('id')) in linked_ids,
        })

    if wants_json:
        return JsonResponse({
            'success': True,
            'account_name': connection.mailchimp_account_name or connection.mailchimp_login_email,
            'candidates': [
                {
                    'campaign_id': item['report'].get('id'),
                    'campaign_title': item['report'].get('campaign_title') or item['report'].get('id'),
                    'subject_line': item['report'].get('subject_line') or '',
                    'send_time': _format_meta_ads_datetime(item['report'].get('send_time')) or 'Unknown',
                    'emails_sent': item['report'].get('emails_sent') or 0,
                    'confidence': item['confidence'],
                    'confidence_pct': item['confidence_pct'],
                    'confidence_class': item['confidence_class'],
                    'reasoning': item['reasoning'],
                    'is_linked': item['is_linked'],
                }
                for item in candidates
            ],
        })

    return render(request, 'tickets/event_mailchimp_match.html', {
        'event': event,
        'candidates': candidates,
        'account_name': connection.mailchimp_account_name or connection.mailchimp_login_email,
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_mailchimp_apply(request, event_id):
    """Pull campaign report details and upsert them as this event's Mailchimp campaign results."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    wants_json = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    connection = _get_mailchimp_connection(org)
    if not connection:
        if wants_json:
            return JsonResponse({'success': False, 'error': 'Connect Mailchimp before applying campaign results.'}, status=400)
        messages.error(request, 'Connect Mailchimp before applying campaign results.')
        return redirect('tickets:mailchimp_settings')

    campaign_ids = [cid.strip() for cid in request.POST.getlist('campaign_id') if cid.strip()]
    if not campaign_ids:
        if wants_json:
            return JsonResponse({'success': False, 'error': 'Choose at least one Mailchimp campaign to apply.'}, status=400)
        messages.error(request, 'Choose at least one Mailchimp campaign to apply.')
        return redirect('tickets:event_mailchimp_match', event_id=event.id)

    confidences = request.POST.getlist('confidence')
    reasonings = request.POST.getlist('reasoning')

    try:
        reports = fetch_org_reports_cached(org)
    except MailchimpAPIError as exc:
        if wants_json:
            return JsonResponse({'success': False, 'error': f'Could not load Mailchimp campaigns: {exc}'}, status=502)
        messages.error(request, f'Could not load Mailchimp campaigns: {exc}')
        return redirect('tickets:event_mailchimp_match', event_id=event.id)
    client = MailchimpClient(connection.mailchimp_access_token, connection.mailchimp_dc)

    available_ids = {str(item.get('id')) for item in reports}
    unknown_ids = [cid for cid in campaign_ids if cid not in available_ids]
    valid_ids = [cid for cid in campaign_ids if cid in available_ids]
    if unknown_ids and not wants_json:
        messages.error(
            request,
            'These Mailchimp campaigns were not found in this account: ' + ', '.join(unknown_ids),
        )

    added_titles = []
    updated_titles = []
    failed_ids = []
    for index, campaign_id in enumerate(valid_ids):
        confidence = confidences[index] if index < len(confidences) else None
        reasoning = (reasonings[index] if index < len(reasonings) else '').strip()
        try:
            report = client.get_campaign_report(campaign_id)
            email_campaign, created = _save_mailchimp_campaign_from_report(
                event,
                report,
                user=request.user,
                match_confidence=confidence or None,
                match_reasoning=reasoning,
            )
        except MailchimpAPIError as exc:
            logger.warning(
                "Mailchimp apply failed for org=%s event=%s campaign=%s: %s",
                org.id, event.id, campaign_id, exc,
            )
            failed_ids.append(campaign_id)
            continue
        if created:
            added_titles.append(email_campaign.campaign_title)
        else:
            updated_titles.append(email_campaign.campaign_title)

    succeeded = len(added_titles) + len(updated_titles)
    if succeeded == 1:
        only_title = (added_titles + updated_titles)[0]
        verb = 'added' if added_titles else 'updated'
        success_msg = f'Mailchimp campaign "{only_title}" {verb} for this event.'
    elif succeeded:
        parts = []
        if added_titles:
            parts.append(f'added {len(added_titles)}')
        if updated_titles:
            parts.append(f'updated {len(updated_titles)}')
        success_msg = f'Linked {succeeded} Mailchimp campaigns to this event ({", ".join(parts)}).'
    else:
        success_msg = ''

    if wants_json:
        all_linked = list(
            EventEmailCampaign.objects.filter(
                event=event,
                source='mailchimp',
                deleted_at__isnull=True,
            ).order_by('-send_time', '-created_at')
        )
        return JsonResponse({
            'success': succeeded > 0,
            'message': success_msg,
            'failed_ids': failed_ids,
            'unknown_ids': unknown_ids,
            'campaigns': [_serialize_mailchimp_campaign(c, event) for c in all_linked],
        })

    if succeeded:
        messages.success(request, success_msg)

    if failed_ids:
        messages.error(
            request,
            'Could not pull results from Mailchimp for: ' + ', '.join(failed_ids),
        )

    if not succeeded and not unknown_ids and not failed_ids:
        messages.error(request, 'No Mailchimp campaigns were applied.')

    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_mailchimp_refresh_all(request, event_id):
    """Refresh all linked Mailchimp campaign reports for an event."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    connection = _get_mailchimp_connection(org)
    if not connection:
        return JsonResponse({
            'success': False,
            'error': 'Connect Mailchimp before refreshing campaign results.',
        }, status=400)

    email_campaigns = list(
        EventEmailCampaign.objects.filter(
            event=event,
            source='mailchimp',
            deleted_at__isnull=True,
        ).exclude(external_id='')
    )
    client = MailchimpClient(connection.mailchimp_access_token, connection.mailchimp_dc)
    had_error = False
    refreshed_campaigns = []

    for email_campaign in email_campaigns:
        try:
            report = client.get_campaign_report(email_campaign.external_id)
            refreshed, _created = _save_mailchimp_campaign_from_report(event, report, user=request.user)
            refreshed_campaigns.append(refreshed)
        except MailchimpAPIError as exc:
            had_error = True
            logger.warning(
                "Mailchimp bulk refresh failed for org=%s event=%s email_campaign=%s campaign=%s: %s",
                org.id,
                event.id,
                email_campaign.id,
                email_campaign.external_id,
                exc,
            )
            refreshed_campaigns.append(email_campaign)

    refreshed_campaigns.sort(key=lambda item: (item.send_time or django_tz.datetime.min.replace(tzinfo=django_tz.utc), item.created_at), reverse=True)
    return JsonResponse({
        'success': True,
        'had_error': had_error,
        'campaigns': [
            _serialize_mailchimp_campaign(email_campaign, event)
            for email_campaign in refreshed_campaigns
        ],
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_mailchimp_refresh(request, event_id, email_campaign_id):
    """Refresh a single linked Mailchimp campaign report."""
    org = get_organization(request)
    event, email_campaign = _get_active_mailchimp_campaign_or_404(org, event_id, email_campaign_id)
    ajax = _wants_marketing_json(request)
    connection = _get_mailchimp_connection(org)
    if not connection:
        msg = 'Connect Mailchimp before refreshing campaign results.'
        if ajax:
            return JsonResponse({'ok': False, 'error': msg}, status=400)
        messages.error(request, msg)
        return redirect('tickets:mailchimp_settings')

    try:
        report = MailchimpClient(connection.mailchimp_access_token, connection.mailchimp_dc).get_campaign_report(email_campaign.external_id)
        refreshed, _created = _save_mailchimp_campaign_from_report(event, report, user=request.user)
    except MailchimpAPIError as exc:
        logger.warning(
            "Mailchimp row refresh failed for org=%s event=%s email_campaign=%s campaign=%s: %s",
            org.id,
            event.id,
            email_campaign.id,
            email_campaign.external_id,
            exc,
        )
        msg = 'Could not refresh this Mailchimp campaign.'
        if ajax:
            return JsonResponse({'ok': False, 'error': msg}, status=502)
        messages.warning(request, msg)
        return _marketing_tab_redirect(event)

    if ajax:
        return JsonResponse({'ok': True, 'row': _serialize_email_row(refreshed)})
    messages.success(request, f'Mailchimp campaign "{refreshed.campaign_title}" refreshed.')
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_mailchimp_remove(request, event_id, email_campaign_id):
    """Unlink a Mailchimp campaign from an event by soft-deleting its stored results."""
    org = get_organization(request)
    event, email_campaign = _get_active_mailchimp_campaign_or_404(org, event_id, email_campaign_id)

    email_campaign.updated_by = request.user
    email_campaign.save(update_fields=['updated_by'])
    email_campaign.delete()

    if _wants_marketing_json(request):
        return JsonResponse({'ok': True, 'removed_id': str(email_campaign.id)})
    messages.success(request, 'Mailchimp campaign removed from this event.')
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_mailchimp_metrics_edit(request, event_id, email_campaign_id):
    """Set or clear manual_clicks/manual_orders/manual_revenue on a Mailchimp campaign.

    Partial update: only fields present in POST are written. Empty string clears
    (sets to NULL → fall back to API). Missing key leaves the field unchanged.
    """
    org = get_organization(request)
    event, email_campaign = _get_active_mailchimp_campaign_or_404(org, event_id, email_campaign_id)
    ajax = _wants_marketing_json(request)
    update_fields = []
    int_fields = ['manual_emails_sent', 'manual_unique_opens', 'manual_clicks', 'manual_unsubscribes', 'manual_orders']
    try:
        for field in int_fields:
            if field in request.POST:
                setattr(email_campaign, field, _parse_optional_int(request.POST.get(field)))
                update_fields.append(field)
        if 'manual_revenue' in request.POST:
            email_campaign.manual_revenue = _parse_optional_decimal(request.POST.get('manual_revenue'))
            update_fields.append('manual_revenue')
    except (ValueError, InvalidOperation):
        msg = 'Enter non-negative numbers (or leave a field blank to use the API value).'
        if ajax:
            return JsonResponse({'ok': False, 'error': msg}, status=400)
        messages.error(request, msg)
        return _marketing_tab_redirect(event)
    if not update_fields:
        if ajax:
            return JsonResponse({'ok': True, 'row': _serialize_email_row(email_campaign)})
        return _marketing_tab_redirect(event)
    email_campaign.updated_by = request.user
    update_fields += ['updated_by', 'updated_at']
    email_campaign.save(update_fields=update_fields)
    _invalidate_marketing_cache(org)
    if ajax:
        return JsonResponse({'ok': True, 'row': _serialize_email_row(email_campaign)})
    messages.success(request, f'Updated metrics for "{email_campaign.campaign_title}".')
    return _marketing_tab_redirect(event)
