"""Meta Ads integration views.

Moved out of tickets/views.py into the integrations package so the
integration is referenced rather than built into the core app.
Shared helpers remain in tickets.views and are imported below.
"""

from ..models import Event
from ..models import EventExpense
from decimal import InvalidOperation
from django.http import JsonResponse
from ..services.meta_ads import MetaAdsAPIError
from ..services.meta_ads import MetaAdsClient
from ..services.meta_campaign_matcher import MetaCampaignMatcher
from django.core.cache import cache as django_cache
from django.utils import timezone as django_tz
from ..services.meta_ads import exchange_for_long_lived_token
from django.shortcuts import get_object_or_404
from ..utils import get_organization
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from ..services.meta_ads import exchange_code_for_token as meta_exchange_code_for_token
from django.shortcuts import redirect
from django.shortcuts import render
from ..utils import require_admin
from django.views.decorators.http import require_http_methods
from ..utils import require_org
from django.urls import reverse
import secrets
from django.conf import settings
from datetime import timedelta
from urllib.parse import urlencode
from ..views import (
    _event_stats_cache_key,
    _format_meta_ads_datetime,
    _get_active_meta_ads_expense_or_404,
    _invalidate_event_list_cache,
    _invalidate_marketing_cache,
    _marketing_tab_redirect,
    _parse_optional_decimal,
    _parse_optional_int,
    _serialize_ads_row,
    _serialize_meta_ads_expense,
    _update_meta_ads_expense_from_insights,
    _wants_marketing_json,
    logger,
)


@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def meta_ads_settings(request):
    """Show Meta Ads connection state for the current org."""
    org = get_organization(request)
    accounts = None
    if org.meta_ads_access_token and not org.meta_ads_account_id:
        try:
            accounts = MetaAdsClient(org.meta_ads_access_token).list_ad_accounts()
        except MetaAdsAPIError as exc:
            messages.error(request, f'Could not load Meta ad accounts: {exc}')

    return render(request, 'tickets/settings_meta_ads.html', {
        'accounts': accounts,
        'callback_url': request.build_absolute_uri(reverse('tickets:meta_ads_callback')),
        'facebook_configured': bool(settings.FACEBOOK_APP_ID and settings.FACEBOOK_APP_SECRET),
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def meta_ads_connect(request):
    """Start Facebook OAuth for Meta Ads Insights access."""
    if not settings.FACEBOOK_APP_ID or not settings.FACEBOOK_APP_SECRET:
        messages.error(request, 'Meta Ads is not configured. Add FACEBOOK_APP_ID and FACEBOOK_APP_SECRET.')
        return redirect('tickets:meta_ads_settings')

    state = secrets.token_urlsafe(32)
    request.session['meta_oauth_state'] = state
    callback_url = request.build_absolute_uri(reverse('tickets:meta_ads_callback'))
    params = urlencode({
        'client_id': settings.FACEBOOK_APP_ID,
        'redirect_uri': callback_url,
        'state': state,
        'scope': 'ads_read,business_management',
        'response_type': 'code',
    })
    return redirect(f'https://www.facebook.com/{settings.FACEBOOK_GRAPH_API_VERSION}/dialog/oauth?{params}')


@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def meta_ads_callback(request):
    """Handle Facebook OAuth callback and persist the long-lived user token."""
    org = get_organization(request)
    expected_state = request.session.pop('meta_oauth_state', None)
    if not expected_state or request.GET.get('state') != expected_state:
        messages.error(request, 'Meta Ads connection could not be verified. Please try again.')
        return redirect('tickets:meta_ads_settings')

    if request.GET.get('error'):
        messages.error(request, request.GET.get('error_description') or 'Meta Ads authorization was cancelled.')
        return redirect('tickets:meta_ads_settings')

    code = request.GET.get('code')
    if not code:
        messages.error(request, 'Meta did not return an authorization code.')
        return redirect('tickets:meta_ads_settings')

    callback_url = request.build_absolute_uri(reverse('tickets:meta_ads_callback'))
    try:
        short_token = meta_exchange_code_for_token(code, callback_url)
        long_token = exchange_for_long_lived_token(short_token['access_token'])
        access_token = long_token['access_token']
        profile = MetaAdsClient(access_token).get_user_profile()
    except (KeyError, MetaAdsAPIError) as exc:
        messages.error(request, f'Could not connect Meta Ads: {exc}')
        return redirect('tickets:meta_ads_settings')

    expires_in = long_token.get('expires_in')
    org.meta_ads_access_token = access_token
    org.meta_ads_user_id = profile.get('id', '')
    org.meta_ads_account_id = ''
    org.meta_ads_account_name = ''
    org.meta_ads_token_expires_at = (
        django_tz.now() + timedelta(seconds=int(expires_in))
        if expires_in else None
    )
    org.save(update_fields=[
        'meta_ads_access_token',
        'meta_ads_user_id',
        'meta_ads_account_id',
        'meta_ads_account_name',
        'meta_ads_token_expires_at',
    ])
    messages.success(request, 'Meta Ads connected. Choose an ad account to finish setup.')
    return redirect('tickets:meta_ads_select_account')


@login_required
@require_org
@require_admin
@require_http_methods(["GET", "POST"])
def meta_ads_select_account(request):
    """Pick the Meta ad account to use for campaign spend."""
    org = get_organization(request)
    if not org.meta_ads_access_token:
        messages.error(request, 'Connect Meta Ads before choosing an ad account.')
        return redirect('tickets:meta_ads_settings')

    try:
        accounts = MetaAdsClient(org.meta_ads_access_token).list_ad_accounts()
    except MetaAdsAPIError as exc:
        messages.error(request, f'Could not load Meta ad accounts: {exc}')
        return redirect('tickets:meta_ads_settings')

    if request.method == 'POST':
        selected_account_id = request.POST.get('account_id', '')
        selected = next((account for account in accounts if account.get('id') == selected_account_id), None)
        if not selected:
            messages.error(request, 'Please choose a valid Meta ad account.')
            return redirect('tickets:meta_ads_select_account')

        org.meta_ads_account_id = selected.get('id', '')
        org.meta_ads_account_name = selected.get('name', '')
        org.save(update_fields=['meta_ads_account_id', 'meta_ads_account_name'])
        messages.success(request, f'Meta Ads account "{org.meta_ads_account_name}" connected.')
        return redirect('tickets:meta_ads_settings')

    return render(request, 'tickets/settings_meta_ads.html', {
        'accounts': accounts,
        'callback_url': request.build_absolute_uri(reverse('tickets:meta_ads_callback')),
        'facebook_configured': bool(settings.FACEBOOK_APP_ID and settings.FACEBOOK_APP_SECRET),
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def meta_ads_disconnect(request):
    """Clear Meta Ads tokens and account selection for the current org."""
    org = get_organization(request)
    org.meta_ads_access_token = ''
    org.meta_ads_user_id = ''
    org.meta_ads_account_id = ''
    org.meta_ads_account_name = ''
    org.meta_ads_token_expires_at = None
    org.save(update_fields=[
        'meta_ads_access_token',
        'meta_ads_user_id',
        'meta_ads_account_id',
        'meta_ads_account_name',
        'meta_ads_token_expires_at',
    ])
    messages.success(request, 'Meta Ads disconnected.')
    return redirect('tickets:meta_ads_settings')


@login_required
@require_org
@require_admin
@require_http_methods(["GET"])
def event_meta_ads_match(request, event_id):
    """Rank Meta campaigns that likely correspond to this event."""
    org = get_organization(request)
    wants_json = request.GET.get('format') == 'json'
    event = get_object_or_404(
        Event.objects.filter(organization=org).select_related('venue'),
        id=event_id,
    )
    if not org.meta_ads_access_token or not org.meta_ads_account_id:
        if wants_json:
            return JsonResponse({
                'success': False,
                'error': 'Connect Meta Ads and choose an ad account before matching campaigns.',
            }, status=400)
        messages.error(request, 'Connect Meta Ads and choose an ad account before matching campaigns.')
        return redirect('tickets:meta_ads_settings')

    try:
        client = MetaAdsClient(org.meta_ads_access_token)
        campaigns = client.list_campaigns(org.meta_ads_account_id)
        match_result = MetaCampaignMatcher(org).rank(event, campaigns)
    except MetaAdsAPIError as exc:
        # Upstream provider error, not a gateway failure on our side — return a
        # handled response so it doesn't log/alert as a 502 "Bad Gateway".
        if wants_json:
            return JsonResponse({'success': False, 'error': f'Could not load Meta campaigns: {exc}'})
        messages.error(request, f'Could not load Meta campaigns: {exc}')
        return redirect('tickets:event_detail', event_id=event.id)
    except Exception as exc:
        logger.exception("Meta campaign matching failed for event %s: %s", event.id, exc)
        if wants_json:
            return JsonResponse({
                'success': False,
                'error': 'Could not rank Meta campaigns. Please check your OpenAI configuration and try again.',
            }, status=500)
        messages.error(request, 'Could not rank Meta campaigns. Please check your OpenAI configuration and try again.')
        return redirect('tickets:event_detail', event_id=event.id)

    campaigns_by_id = {str(campaign.get('id')): campaign for campaign in campaigns}
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
        candidates.append({
            'campaign': campaign,
            'confidence': candidate.confidence,
            'confidence_pct': confidence_pct,
            'confidence_class': confidence_class,
            'reasoning': candidate.reasoning,
        })

    if wants_json:
        return JsonResponse({
            'success': True,
            'account_name': org.meta_ads_account_name,
            'candidates': [
                {
                    'campaign_id': item['campaign'].get('id'),
                    'campaign_name': item['campaign'].get('name') or item['campaign'].get('id'),
                    'objective': item['campaign'].get('objective') or 'No objective',
                    'start_time': _format_meta_ads_datetime(
                        item['campaign'].get('start_time') or item['campaign'].get('created_time')
                    ) or 'Unknown',
                    'stop_time': _format_meta_ads_datetime(item['campaign'].get('stop_time')) or 'Not set',
                    'confidence_pct': item['confidence_pct'],
                    'confidence_class': item['confidence_class'],
                    'reasoning': item['reasoning'],
                }
                for item in candidates
            ],
        })

    return render(request, 'tickets/event_meta_ads_match.html', {
        'event': event,
        'candidates': candidates,
        'account_name': org.meta_ads_account_name,
    })


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_meta_ads_apply(request, event_id):
    """Pull campaign lifetime spend and upsert it as this event's Meta Ads expense."""
    org = get_organization(request)
    event = get_object_or_404(Event.objects.filter(organization=org), id=event_id)
    wants_json = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    def _json_error(msg, status):
        return JsonResponse({'success': False, 'error': msg}, status=status)

    if not org.meta_ads_access_token or not org.meta_ads_account_id:
        if wants_json:
            return _json_error('Connect Meta Ads and choose an ad account before applying campaign spend.', 400)
        messages.error(request, 'Connect Meta Ads and choose an ad account before applying campaign spend.')
        return redirect('tickets:meta_ads_settings')

    campaign_id = request.POST.get('campaign_id', '').strip()
    if not campaign_id:
        if wants_json:
            return _json_error('Choose a Meta campaign to apply.', 400)
        messages.error(request, 'Choose a Meta campaign to apply.')
        return redirect('tickets:event_meta_ads_match', event_id=event.id)

    try:
        client = MetaAdsClient(org.meta_ads_access_token)
        campaigns = client.list_campaigns(org.meta_ads_account_id)
        campaign = next((item for item in campaigns if str(item.get('id')) == campaign_id), None)
        if not campaign:
            if wants_json:
                return _json_error('The selected Meta campaign was not found in this ad account.', 404)
            messages.error(request, 'The selected Meta campaign was not found in this ad account.')
            return redirect('tickets:event_meta_ads_match', event_id=event.id)
        insights = client.get_campaign_insights(campaign_id)
    except MetaAdsAPIError as exc:
        if wants_json:
            return _json_error(f'Could not pull campaign spend from Meta: {exc}', 502)
        messages.error(request, f'Could not pull campaign spend from Meta: {exc}')
        return redirect('tickets:event_meta_ads_match', event_id=event.id)

    spend = insights.spend
    campaign_name = campaign.get('name') or campaign_id
    expense, created = EventExpense.objects.get_or_create(
        event=event,
        source='meta_ads',
        external_id=campaign_id,
        deleted_at__isnull=True,
        defaults={
            'category': 'marketing',
            'description': f'Meta Ads: {campaign_name}'[:300],
            'amount': spend,
            'api_attributed_orders': insights.purchases,
            'api_attributed_revenue': insights.purchase_value,
            'expense_date': event.start_date,
            'external_metadata': {},
            'created_by': request.user,
        },
    )
    if not created:
        api_changed = (
            expense.amount != spend
            or expense.api_attributed_orders != insights.purchases
            or expense.api_attributed_revenue != insights.purchase_value
        )
        was_confirmed = bool(expense.confirmed_at)
        expense.category = 'marketing'
        expense.description = f'Meta Ads: {campaign_name}'[:300]
        expense.amount = spend
        expense.api_attributed_orders = insights.purchases
        expense.api_attributed_revenue = insights.purchase_value
        expense.expense_date = expense.expense_date or event.start_date
        expense.external_id = campaign_id
        expense.updated_by = request.user
        expense.version += 1
        if was_confirmed and api_changed:
            expense.api_data_changed_at = django_tz.now()

    expense.external_metadata = {
        'campaign_name': campaign_name,
        'ad_account_id': org.meta_ads_account_id,
        'ad_account_name': org.meta_ads_account_name,
        'last_synced_at': django_tz.now().isoformat(),
    }
    expense.save()
    _invalidate_event_list_cache(org)
    _invalidate_marketing_cache(org)

    action = 'updated' if not created else 'added'
    success_msg = f'Meta Ads campaign spend ${spend:,.2f} {action} as a linked marketing expense.'

    if wants_json:
        linked = list(
            EventExpense.objects.filter(
                event=event,
                source='meta_ads',
                deleted_at__isnull=True,
            ).order_by('-created_at')
        )
        return JsonResponse({
            'success': True,
            'message': success_msg,
            'expenses': [_serialize_meta_ads_expense(item, event) for item in linked],
        })

    messages.success(request, success_msg)
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_meta_ads_refresh(request, event_id, expense_id):
    """Refresh a single linked Meta Ads campaign expense."""
    org = get_organization(request)
    event, expense = _get_active_meta_ads_expense_or_404(org, event_id, expense_id)
    ajax = _wants_marketing_json(request)

    if not org.meta_ads_access_token or not org.meta_ads_account_id:
        msg = 'Connect Meta Ads and choose an ad account before refreshing campaign spend.'
        if ajax:
            return JsonResponse({'ok': False, 'error': msg}, status=400)
        messages.error(request, msg)
        return redirect('tickets:meta_ads_settings')

    try:
        insights = MetaAdsClient(org.meta_ads_access_token).get_campaign_insights(expense.external_id)
    except MetaAdsAPIError as exc:
        logger.warning(
            "Meta Ads row refresh failed for org=%s event=%s expense=%s campaign=%s: %s",
            org.id,
            event.id,
            expense.id,
            expense.external_id,
            exc,
        )
        msg = f'Could not refresh this Meta Ads campaign spend: {exc}'
        # Upstream provider error, not a gateway failure on our side — return a
        # handled response so it doesn't log/alert as a 502 "Bad Gateway".
        if ajax:
            return JsonResponse({'ok': False, 'error': msg})
        messages.warning(request, msg)
        return _marketing_tab_redirect(event)

    api_changed = _update_meta_ads_expense_from_insights(expense, insights, request.user)
    django_cache.delete(_event_stats_cache_key(event.pk))
    if api_changed:
        _invalidate_event_list_cache(org)
        _invalidate_marketing_cache(org)

    if ajax:
        expense.refresh_from_db()
        return JsonResponse({'ok': True, 'row': _serialize_ads_row(expense)})
    messages.success(request, f'Meta Ads campaign spend refreshed to ${insights.spend:,.2f}.')
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_meta_ads_remove(request, event_id, expense_id):
    """Unlink a Meta Ads campaign from an event by soft-deleting its expense."""
    org = get_organization(request)
    event, expense = _get_active_meta_ads_expense_or_404(org, event_id, expense_id)

    expense.updated_by = request.user
    expense.save(update_fields=['updated_by'])
    expense.delete()
    django_cache.delete(_event_stats_cache_key(event.pk))
    _invalidate_event_list_cache(org)
    _invalidate_marketing_cache(org)

    if _wants_marketing_json(request):
        return JsonResponse({'ok': True, 'removed_id': str(expense.id)})
    messages.success(request, 'Meta Ads campaign removed from this event.')
    return _marketing_tab_redirect(event)


@login_required
@require_org
@require_admin
@require_http_methods(["POST"])
def event_meta_ads_metrics_edit(request, event_id, expense_id):
    """Partial update of manual_attributed_orders / manual_attributed_revenue."""
    org = get_organization(request)
    event, expense = _get_active_meta_ads_expense_or_404(org, event_id, expense_id)
    ajax = _wants_marketing_json(request)
    update_fields = []
    try:
        if 'manual_attributed_orders' in request.POST:
            expense.manual_attributed_orders = _parse_optional_int(request.POST.get('manual_attributed_orders'))
            update_fields.append('manual_attributed_orders')
        if 'manual_attributed_revenue' in request.POST:
            expense.manual_attributed_revenue = _parse_optional_decimal(request.POST.get('manual_attributed_revenue'))
            update_fields.append('manual_attributed_revenue')
    except (ValueError, InvalidOperation):
        msg = 'Enter non-negative numbers for attributed orders and revenue.'
        if ajax:
            return JsonResponse({'ok': False, 'error': msg}, status=400)
        messages.error(request, msg)
        return _marketing_tab_redirect(event)
    if not update_fields:
        if ajax:
            return JsonResponse({'ok': True, 'row': _serialize_ads_row(expense)})
        return _marketing_tab_redirect(event)
    expense.updated_by = request.user
    update_fields += ['updated_by', 'updated_at']
    expense.save(update_fields=update_fields)
    _invalidate_marketing_cache(org)
    if ajax:
        return JsonResponse({'ok': True, 'row': _serialize_ads_row(expense)})
    messages.success(request, 'Updated ad attribution.')
    return _marketing_tab_redirect(event)
