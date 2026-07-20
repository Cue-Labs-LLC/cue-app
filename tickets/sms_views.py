"""Views for native marketing SMS: campaigns, recipient lists, and Twilio webhooks.

Kept out of the (very large) views.py for cohesion. Every authenticated view is
org-scoped and gated behind the per-org ``sms_marketing_enabled`` flag.
"""

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone as stdlib_tz
from decimal import Decimal
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction, IntegrityError
from django.db.models import Count, Q, F, Sum
from django.db.models.functions import Coalesce, Now
from django.http import JsonResponse, HttpResponse, Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode, url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods

from .models import (
    SMSCampaign, SMSCampaignPlan, SMSMessageRecipient, PhoneSuppression, SMSConsentRecord, Event,
    Customer, CustomerTag, TrackingLink, StripeCheckoutSession, Ticket,
    EventSMSCampaign,
    TICKETING_TYPE_DIRECT, TICKETING_TYPE_EXTERNAL, EVENT_STATUS_LIVE,
    _generate_tracking_token,
)
from .forms import SMSCampaignForm, SMS_SEGMENT_CHOICES
from .services.customer_filters import filter_customers, _valid_uuids, market_filter_options, NO_MARKET_VALUE
from .services.sms_consent import set_sms_opt_in
from .services.tagging import tag_customers
from .sms import (
    normalize_phone, validate_twilio_request, sms_segment_info, send_sms, extract_first_url,
    with_stop_footer, TWILIO_OPT_OUT_ERROR_CODES,
)
from .tasks import send_sms_campaign_task
from .utils import get_organization, require_org, require_host

logger = logging.getLogger(__name__)


def require_sms_feature(view):
    """Gate a view behind the org's sms_marketing_enabled flag (pilot rollout)."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        org = get_organization(request)
        if not org or not org.sms_marketing_enabled:
            raise Http404('SMS marketing is not enabled for this organization.')
        return view(request, *args, **kwargs)
    return wrapped


# Status groupings for derived campaign counts (SMSMessageRecipient is the
# source of truth — counts are computed, never incremented, so retried Twilio
# callbacks cannot cause drift).
_HANDED_OFF = ['sent', 'delivered', 'undelivered']
_FAILED = ['failed', 'undelivered']


def _annotate_counts(qs):
    # All annotations are over the same `recipients` join, so combining Count
    # filters with a Sum here does not cause join inflation.
    return qs.annotate(
        sent_count=Count('recipients', filter=Q(recipients__status__in=_HANDED_OFF)),
        delivered_count=Count('recipients', filter=Q(recipients__status='delivered')),
        failed_count=Count('recipients', filter=Q(recipients__status__in=_FAILED)),
        unique_clicks=Count('recipients', filter=Q(recipients__first_clicked_at__isnull=False)),
        total_clicks=Coalesce(Sum('recipients__click_count'), 0),
        unsub_count=Count('recipients', filter=Q(recipients__opted_out_at__isnull=False)),
    )


def _criteria_from_post(post):
    """Build an inline filter_criteria dict + manual id lists from raw POST data
    (used by the live audience-preview endpoint, which fires before the campaign
    is saved). The compose UI offers tags + segments; event mode passes `event`
    plus an `audience_scope` of 'event' (ticket buyers), 'all' (all subscribers),
    or 'tag' (customers with the chosen tag)."""
    criteria = {}
    segments = [s for s in post.getlist('rfm_segment') if s]
    if segments:
        criteria['rfm_segment'] = segments
    tag_ids = [t for t in post.getlist('tag_ids') if t]
    if tag_ids:
        criteria['tag_ids'] = tag_ids
    market_id = (post.get('market_id') or '').strip()
    if market_id:
        criteria['market_id'] = market_id
    event_id = (post.get('event') or '').strip()
    if event_id:
        # Event mode is a single-choice audience; the scope picks exactly one
        # narrowing, so ignore any stray tag/segment values from hidden controls.
        scope = post.get('audience_scope') or 'event'
        if scope == 'all':
            criteria = {'all_subscribers': True}
        elif scope == 'tag':
            criteria = {'tag_ids': tag_ids} if tag_ids else {}
        else:
            criteria = {'event_id': event_id}
    includes = [s.strip() for s in (post.get('manual_include_ids') or '').split(',') if s.strip()]
    excludes = [s.strip() for s in (post.get('manual_exclude_ids') or '').split(',') if s.strip()]
    return criteria, includes, excludes


def _customer_list_criteria_from_post(post):
    sms_f = post.get('sms_filter', '')
    return {
        'search': post.get('search') or None,
        'rfm_segment': post.get('segment') or None,
        'tag_id': post.get('tag') or None,
        'market_id': post.get('market') or None,
        'last_order_after': post.get('last_order_from') or None,
        'last_order_before': post.get('last_order_to') or None,
        'phone': post.get('phone_filter') or None,
        'sms_opt_in': True if sms_f == '1' else (False if sms_f == '0' else None),
        'max_ltv': post.get('max_ltv') or None,
    }


def _customer_list_back_url(post):
    params = urlencode({k: v for k, v in (
        ('search', post.get('search', '')),
        ('segment', post.get('segment', '')),
        ('tag', post.get('tag', '')),
        ('market', post.get('market', '')),
        ('last_order_from', post.get('last_order_from', '')),
        ('last_order_to', post.get('last_order_to', '')),
        ('phone_filter', post.get('phone_filter', '')),
        ('sms_filter', post.get('sms_filter', '')),
        ('min_ltv', post.get('min_ltv', '')),
        ('max_ltv', post.get('max_ltv', '')),
        ('min_orders', post.get('min_orders', '')),
        ('max_orders', post.get('max_orders', '')),
        ('sort', post.get('sort', '')),
    ) if v})
    return reverse('tickets:customer_list') + (f'?{params}' if params else '')


@login_required
@require_org
@require_host
@require_sms_feature
@require_POST
def sms_audience_preview(request):
    """JSON: resolved recipient count for the audience being composed (live sizing).
    Builds a transient SMSCampaign from the posted criteria — never saved."""
    org = get_organization(request)
    criteria, includes, excludes = _criteria_from_post(request.POST)
    cap = getattr(settings, 'SMS_CAMPAIGN_MAX_RECIPIENTS', 5000)
    if not criteria and not includes:
        return JsonResponse({'count': 0, 'exceeds_cap': False, 'cap': cap})
    tmp = SMSCampaign(
        organization=org, filter_criteria=criteria,
        manual_include_ids=includes, manual_exclude_ids=excludes,
    )
    recipients = tmp.materialize(org, cap=cap + 1)
    return JsonResponse({
        'count': min(len(recipients), cap),
        'exceeds_cap': len(recipients) > cap,
        'cap': cap,
    })


# ---------------------------------------------------------------------------
# Bulk tagging (customer list + event Customers tab)
# ---------------------------------------------------------------------------

@login_required
@require_org
@require_host
@require_POST
def event_bulk_tag(request, event_id):
    """Apply a tag (existing or new) to selected, or all, buyers of an event."""
    org = get_organization(request)
    event = get_object_or_404(
        Event.objects.filter(organization=org, deleted_at__isnull=True), id=event_id,
    )
    attendees = Customer.objects.filter(organization=org, ticket_orders__event=event)
    if request.POST.get('select_all') == '1':
        customers = attendees.distinct()
    else:
        posted = [s for s in request.POST.getlist('customer_ids') if s]
        customers = attendees.filter(id__in=posted).distinct()

    redirect_to = redirect(
        reverse('tickets:event_detail', args=[event.id]) + '?tab=customers'
    )
    tag, count = tag_customers(
        org, customers,
        tag_id=request.POST.get('tag_id'),
        new_tag_name=request.POST.get('new_tag_name'),
    )
    if tag is None:
        messages.error(request, 'Choose an existing tag or enter a new tag name.')
    elif count == 0:
        messages.error(request, 'No customers selected.')
    else:
        messages.success(request, f'Tagged {count} customer(s) as "{tag.name}".')
    return redirect_to


@login_required
@require_org
@require_host
@require_POST
def customers_bulk_tag(request):
    """Apply a tag (existing or new) to selected, or all-matching, customers from
    the org-wide customer list. 'Select all' resolves the same filtered queryset
    the list page shows (search / segment / tag)."""
    org = get_organization(request)
    if request.POST.get('select_all') == '1':
        customers = filter_customers(org, _customer_list_criteria_from_post(request.POST)).distinct()
    else:
        posted = [s for s in request.POST.getlist('customer_ids') if s]
        customers = Customer.objects.filter(organization=org, id__in=posted).distinct()

    # Return to the list with the active filters preserved.
    back = _customer_list_back_url(request.POST)
    tag, count = tag_customers(
        org, customers,
        tag_id=request.POST.get('tag_id'),
        new_tag_name=request.POST.get('new_tag_name'),
    )
    if tag is None:
        messages.error(request, 'Choose an existing tag or enter a new tag name.')
    elif count == 0:
        messages.error(request, 'No customers selected.')
    else:
        messages.success(request, f'Tagged {count} customer(s) as "{tag.name}".')
    return redirect(back)


@login_required
@require_org
@require_host
@require_POST
def customers_bulk_sms_status(request):
    """Bulk opt selected, or all-matching, customers in or out of marketing SMS.
    'Select all' resolves the same filtered queryset the list page shows
    (search / segment / tag), mirroring ``customers_bulk_tag``."""
    org = get_organization(request)
    if request.POST.get('select_all') == '1':
        customers = filter_customers(org, _customer_list_criteria_from_post(request.POST)).distinct()
    else:
        posted = [s for s in request.POST.getlist('customer_ids') if s]
        customers = Customer.objects.filter(organization=org, id__in=posted).distinct()

    # Return to the list with the active filters preserved.
    back = _customer_list_back_url(request.POST)

    opt_in = request.POST.get('sms_status') == 'opt_in'
    count = set_sms_opt_in(customers, opt_in=opt_in)
    verb = 'Opted in' if opt_in else 'Opted out'
    if count == 0:
        # Either nothing was selected or every selected customer was already in
        # the target state — nothing actually changed either way.
        messages.info(request, 'No customers needed updating.')
    else:
        messages.success(request, f'{verb} {count} customer(s).')

    # Opting in can't override a STOP: a number that replied STOP stays suppressed
    # (Twilio + our mirror) and won't be texted until the person texts START — an
    # organizer can't re-consent on their behalf. Flag any so the opt-in isn't a
    # silent no-op. Counts distinct phones on the selection against the suppression
    # list in one query.
    if opt_in:
        suppressed = PhoneSuppression.suppressed_phones(org)
        if suppressed:
            blocked = len({
                normalize_phone(p) for p in customers.values_list('phone', flat=True)
                if normalize_phone(p) in suppressed
            })
            if blocked:
                messages.warning(
                    request,
                    f"{blocked} of these opted out via STOP and can only re-subscribe by "
                    f"texting START — they won't receive messages until they do."
                )
    return redirect(back)


@login_required
@require_org
@require_host
@require_sms_feature
@require_POST
def customers_bulk_sms_compose(request):
    """Capture selected customer IDs and open the campaign composer pre-targeted."""
    org = get_organization(request)
    cap = getattr(settings, 'SMS_CAMPAIGN_MAX_RECIPIENTS', 5000)

    if request.POST.get('select_all') == '1':
        qs = filter_customers(org, _customer_list_criteria_from_post(request.POST)).distinct()
        ids = [str(i) for i in qs.values_list('id', flat=True)[:cap]]
    else:
        posted = _valid_uuids(s for s in request.POST.getlist('customer_ids') if s)
        ids = [
            str(i) for i in
            Customer.objects.filter(organization=org, id__in=posted)
            .values_list('id', flat=True)
        ]

    back = _customer_list_back_url(request.POST)

    if not ids:
        messages.warning(request, 'No customers selected.')
        return redirect(back)

    n = len(ids)
    request.session['sms_compose_prefill'] = {
        'ids': ids,
        'count': n,
        'label': f'{n} customer{"s" if n != 1 else ""}',
    }
    return redirect('tickets:sms_campaign_create')


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

def _org_events_for_picker(org):
    """Lightweight (id, name, start_date) list of the org's events, newest first —
    embedded in the "link to event" picker for client-side typeahead. The date
    disambiguates events that share a name. Mirrors the ticket_link_events
    approach in sms_campaign_create."""
    return [
        {'id': str(e.id), 'name': e.name, 'start_date': e.start_date}
        for e in Event.objects.filter(organization=org, deleted_at__isnull=True)
                              .order_by('-start_date')
                              .values_list('id', 'name', 'start_date', named=True)
    ]


@login_required
@require_org
@require_host
def sms_campaign_list(request):
    import json
    from .services.marketing import (
        get_cached_marketing_metrics, WINDOW_CHOICES, resolve_window, DEFAULT_WINDOW,
    )
    org = get_organization(request)
    native = org.sms_marketing_enabled

    # Consolidated SMS performance band — reuses the Marketing analytics plumbing
    # (same cache/window as the Overview page) so all SMS lives on one page.
    window_key, window_days, window_label = resolve_window(request.GET.get('window', DEFAULT_WINDOW))
    metrics = get_cached_marketing_metrics(org, window_days, window_key)
    now = timezone.now()

    # Build unified campaigns list: native Cue sends + external (SlickText etc.)
    # Both are window-filtered for consistency.
    native_qs = _annotate_counts(
        SMSCampaign.objects.filter(
            organization=org, deleted_at__isnull=True, status=SMSCampaign.Status.SENT,
        ).select_related('event')
    )
    if window_days:
        native_qs = native_qs.filter(sent_at__gte=now - timedelta(days=window_days))

    external_qs = EventSMSCampaign.objects.filter(
        event__organization=org, send_time__isnull=False,
    ).select_related('event')
    if window_days:
        external_qs = external_qs.filter(send_time__gte=now - timedelta(days=window_days))

    unified_campaigns = []
    for c in native_qs:
        unified_campaigns.append({
            'source_label': 'Cue',
            'source_key': 'cue',
            'name': c.name,
            'detail_url': reverse('tickets:sms_campaign_detail', args=[c.id]),
            'event': c.event,
            'audience': c.audience_size or 0,
            'clicks': c.unique_clicks,
            'orders': None,
            'revenue': None,
            'when': c.sent_at or c.scheduled_at,
            'message': c.body,
            'media_url': '',
        })
    for c in external_qs:
        label = (c.source or 'slicktext').replace('_', ' ').title()
        unified_campaigns.append({
            'source_label': label,
            'source_key': (c.source or 'slicktext'),
            'name': c.name or 'Untitled',
            'detail_url': None,
            'event': c.event,
            'audience': c.effective_audience,
            'clicks': c.effective_clicks,
            'orders': c.effective_orders,
            'revenue': c.effective_revenue,
            'when': c.send_time,
            'message': c.message,
            'media_url': c.media_url,
        })

    _epoch = datetime.min.replace(tzinfo=stdlib_tz.utc)
    unified_campaigns.sort(key=lambda b: b['when'] or _epoch, reverse=True)
    paginator = Paginator(unified_campaigns, 25)
    campaigns_page = paginator.get_page(request.GET.get('page'))

    # Upcoming band: native campaigns that are scheduled but not yet sent, soonest first.
    # Not window-filtered — future sends should always be visible regardless of the
    # analytics window — so the organizer can find (and cancel) a queued send.
    scheduled_campaigns = list(
        SMSCampaign.objects.filter(
            organization=org, deleted_at__isnull=True,
            status=SMSCampaign.Status.SCHEDULED,
        ).select_related('event').order_by('scheduled_at')
    )

    # In-progress band: native campaigns actively sending — or stuck mid-send after
    # a chunk errored (status stays 'sending' until the cron recovery pass finishes
    # it). Not window-filtered (sent_at is null while sending) so an in-flight send
    # is always visible; annotated so the table can show sent-so-far vs audience,
    # which is what distinguishes a healthy send from a stalled one. Newest first.
    sending_campaigns = list(
        _annotate_counts(
            SMSCampaign.objects.filter(
                organization=org, deleted_at__isnull=True,
                status=SMSCampaign.Status.SENDING,
            ).select_related('event')
        ).order_by('-started_at')
    )

    # Broadcast audience over time + by market. The cached series is
    # market-independent; the market filter is applied here in Python.
    series = metrics['broadcast_audience']
    selected_market = request.GET.get('market', '')
    # Only list cities that actually have broadcasts; 'No market' sorts last.
    market_choices = sorted(
        {row['market'] for row in series},
        key=lambda m: (m == 'No market', m.lower()),
    )
    if selected_market and selected_market not in market_choices:
        selected_market = ''

    # By-market breakdown spans ALL markets, independent of the chart filter.
    # Computed first so auto-selection can use avg_audience.
    breakdown = {}
    for r in series:
        agg = breakdown.setdefault(
            r['market'], {
                'market_id': r.get('market_id', ''),
                'market_name': r.get('market_name') or r['market'],
                'market_label': r.get('market_label') or r['market'],
                'market': r['market'],
                'broadcasts': 0,
                'total_audience': 0,
            },
        )
        agg['broadcasts'] += 1
        agg['total_audience'] += r['audience']
    market_breakdown = sorted(breakdown.values(), key=lambda a: a['total_audience'], reverse=True)
    for agg in market_breakdown:
        agg['avg_audience'] = round(agg['total_audience'] / agg['broadcasts']) if agg['broadcasts'] else 0

    # Default to the market with the highest avg audience when none is explicitly
    # chosen. An explicit ?market= (empty string) opts into the "All Markets" view.
    if 'market' not in request.GET and market_breakdown:
        selected_market = max(market_breakdown, key=lambda a: a['avg_audience'])['market']

    visible = [r for r in series if not selected_market or r['market'] == selected_market]
    if selected_market:
        by_market = {}
        for r in visible:
            by_market.setdefault(r['market'], []).append({
                'x': r['sent_ms'], 'y': r['audience'],
                'name': r['name'], 'market': r['market'],
            })
        market_order = sorted(by_market, key=lambda m: sum(p['y'] for p in by_market[m]), reverse=True)
    else:
        all_pts = sorted(
            [{'x': r['sent_ms'], 'y': r['audience'], 'name': r['name'], 'market': r['market']} for r in visible],
            key=lambda p: p['x'],
        )
        by_market = {'All Markets': all_pts}
        market_order = ['All Markets']
    audience_points = {'by_market': by_market, 'market_order': market_order}

    # Sub-views: Campaigns (unified) + Audience — same for all orgs.
    sms_views = ['campaigns', 'audience']
    view = request.GET.get('view', 'campaigns').lower()
    if view not in sms_views:
        view = 'campaigns'

    return render(request, 'tickets/marketing/sms/campaign_list.html', {
        'campaigns_page': campaigns_page,
        'scheduled_campaigns': scheduled_campaigns,
        'sending_campaigns': sending_campaigns,
        'balance_cents': org.sms_credit_balance_cents,
        'sms_native_enabled': native,
        'marketing_section': 'sms',
        'sms_views': sms_views,
        'view': view,
        'window_choices': WINDOW_CHOICES,
        'window_key': window_key,
        'window_label': window_label,
        'native_sms': metrics['native_sms'],
        'sms_channel': metrics['channels']['sms'],
        'top_sms_campaigns': metrics['top_sms_campaigns'],
        'selected_market': selected_market,
        'market_choices': market_choices,
        'market_breakdown': market_breakdown,
        'audience_points_json': json.dumps(audience_points),
        'link_events': _org_events_for_picker(org),
    })


@login_required
@require_org
@require_host
@require_sms_feature
@require_http_methods(['GET', 'POST'])
def sms_campaign_create(request):
    """Compose + send/schedule. A valid POST first shows a confirmation panel with
    the resolved recipient count; only a second POST with `confirm` dispatches.

    Event mode: with ?event=<id> (or a hidden `event` field on POST), the audience
    is pre-set to that event's attendees and the campaign is linked to the event."""
    org = get_organization(request)
    cap = getattr(settings, 'SMS_CAMPAIGN_MAX_RECIPIENTS', 5000)
    confirm_count = None
    exceeds_cap = False
    confirm_cost_cents = None
    confirm_cost_tokens = None
    insufficient_credits = False
    prefill = None
    strategist_prefill = None
    manual_include_ids = []

    # Customer-list bulk SMS starts as a session handoff on GET, then persists
    # through review/confirm via a hidden field on POST. The AI strategist reuses
    # the same handoff but carries a written body + audience criteria (no manual ids).
    if request.method == 'GET':
        handoff = request.session.pop('sms_compose_prefill', None)
        if handoff and handoff.get('ids'):
            prefill = handoff
            manual_include_ids = list(handoff.get('ids') or [])
        elif handoff:
            strategist_prefill = handoff
    else:
        raw_manual_ids = request.POST.get('manual_include_ids', '')
        manual_include_ids = _valid_uuids(raw_manual_ids.split(','))
        if manual_include_ids:
            n = len(manual_include_ids)
            prefill = {
                'ids': manual_include_ids,
                'count': n,
                'label': f'{n} customer{"s" if n != 1 else ""}',
            }
    has_manual = bool(manual_include_ids)

    event = None
    event_id = request.POST.get('event') or request.GET.get('event')
    if event_id:
        event = get_object_or_404(
            Event.objects.filter(organization=org, deleted_at__isnull=True), id=event_id,
        )

    # Per-submit idempotency token: generated when the confirm panel renders and
    # echoed back on confirm, so a double-click / browser retry can't create a
    # second campaign or double-charge. Preserved across the review→confirm POSTs.
    idem_key = request.POST.get('idempotency_key') or uuid.uuid4().hex

    # Event-mode audience scope. Read outside form handling (and normalized) so the
    # re-rendered confirm page can keep the chosen chip checked — otherwise the
    # selection silently resets to 'event' between review and confirm. Also honored
    # from the query string so a launched plan step can preselect the right chip
    # (e.g. an "all subscribers" step opens on the All SMS subscribers scope).
    audience_scope = request.POST.get('audience_scope') or request.GET.get('audience_scope') or 'event'
    if audience_scope not in ('event', 'all', 'tag'):
        audience_scope = 'event'

    if request.method == 'POST':
        form = SMSCampaignForm(
            request.POST, organization=org, event=event,
            has_manual_includes=has_manual,
        )
        if form.is_valid():
            # Audience lives inline on the campaign. In event mode the scope picks
            # exactly one audience — the event's ticket buyers, all subscribers, or
            # customers with the chosen tag; otherwise the composed tags/segments.
            criteria = dict(form.filter_criteria)
            if event:
                if audience_scope == 'all':
                    criteria = {'all_subscribers': True}
                elif audience_scope == 'tag':
                    tag_ids = [str(t.id) for t in form.cleaned_data.get('tag_ids') or []]
                    criteria = {'tag_ids': tag_ids} if tag_ids else {}
                else:
                    criteria = {'event_id': str(event.id)}
            recipients = SMSCampaign(
                organization=org, filter_criteria=criteria,
                manual_include_ids=manual_include_ids,
            ).materialize(org, cap=cap + 1)
            confirm_count = len(recipients)
            exceeds_cap = confirm_count > cap

            if exceeds_cap:
                form.add_error(
                    None,
                    f'This audience resolves to more than {cap} recipients. '
                    f'Narrow the audience before sending.',
                )
            elif event and audience_scope == 'tag' and not criteria.get('tag_ids'):
                form.add_error(None, 'Pick at least one tag to send to.')
            elif confirm_count == 0:
                form.add_error(None, 'This audience has no contactable recipients.')
            else:
                from .services.sms_credits import plan_campaign_footers, InsufficientCreditsError
                # Anchor the per-recipient footer/disclosure decision (and therefore the
                # cost) on the actual send time: a far-future scheduled send must re-disclose
                # phones that will have aged out of the window by then. Computed once here
                # for the confirm-panel display; finalize_campaign_send recomputes the same
                # way for the charge → displayed == charged.
                scheduled = form.cleaned_data.get('send_mode') == SMSCampaignForm.SEND_SCHEDULE
                send_at = form.cleaned_data['scheduled_at'] if scheduled else timezone.now()
                confirm_cost_cents, footer_plan = plan_campaign_footers(
                    org, form.cleaned_data['body'],
                    [r['phone'] for r in recipients], as_of=send_at,
                )
                # Displayed tokens (1 token = 1 segment) = the charged segments, summed per
                # recipient so a shared phone counted twice in the audience is shown twice.
                confirm_cost_tokens = sum(
                    footer_plan[r['phone']][1] for r in recipients
                )
                insufficient_credits = confirm_cost_cents > org.sms_credit_balance_cents

                if request.POST.get('confirm') and insufficient_credits:
                    form.add_error(
                        None,
                        'Not enough SMS tokens to send this campaign. Top up to continue.',
                    )
                elif request.POST.get('confirm'):
                    from .services.sms_campaigns import (
                        finalize_campaign_send, AudienceEmptyError, AudienceTooLargeError,
                    )
                    try:
                        result = finalize_campaign_send(
                            org, name=form.cleaned_data['name'],
                            body=form.cleaned_data['body'], criteria=criteria,
                            manual_include_ids=manual_include_ids, event=event,
                            scheduled=scheduled, send_at=send_at, user=request.user,
                            idempotency_key=idem_key, cap=cap,
                        )
                    except InsufficientCreditsError:
                        insufficient_credits = True
                        form.add_error(
                            None,
                            'Not enough SMS tokens to send this campaign. Top up to continue.',
                        )
                    except AudienceEmptyError:
                        form.add_error(None, 'This audience has no contactable recipients.')
                    except AudienceTooLargeError:
                        form.add_error(
                            None,
                            f'This audience resolves to more than {cap} recipients. '
                            f'Narrow the audience before sending.',
                        )
                    else:
                        # A send that originated from an AI plan step marks that step
                        # launched + links it to the new campaign — only on a real send.
                        if result.created:
                            plan_id = request.POST.get('prefill_plan_id')
                            step_raw = request.POST.get('prefill_step')
                            if plan_id and step_raw not in (None, ''):
                                try:
                                    _mark_plan_step_launched(
                                        org, plan_id, int(step_raw), result.campaign.id,
                                    )
                                except (ValueError, TypeError):
                                    pass
                        else:
                            return redirect('tickets:sms_campaign_detail', pk=result.campaign.id)
                        if not result.scheduled:
                            messages.success(
                                request,
                                f'Sending "{result.campaign.name}" to {result.recipient_count} recipients.',
                            )
                        else:
                            messages.success(
                                request,
                                f'Campaign scheduled for {result.campaign.scheduled_at:%b %d, %Y %I:%M %p}.',
                            )
                        return redirect('tickets:sms_campaign_detail', pk=result.campaign.id)
    else:
        if strategist_prefill:
            # AI plan step: prefill the written body + audience selection so the
            # organizer only has to review, confirm cost, and send.
            initial = {
                'name': strategist_prefill.get('name') or (event.name if event else 'SMS'),
                'body': strategist_prefill.get('body', ''),
            }
            criteria = strategist_prefill.get('criteria') or {}
            if not event:
                if criteria.get('rfm_segment'):
                    initial['rfm_segment'] = criteria['rfm_segment']
                if criteria.get('tag_ids'):
                    initial['tag_ids'] = criteria['tag_ids']
                # The composer's market field is single-select; a plan step targets one
                # market but may store it under either key (market_ids from the auto plan,
                # market_id from the plan audience editor) — accept both.
                if criteria.get('market_ids'):
                    initial['market_id'] = criteria['market_ids'][0]
                elif criteria.get('market_id'):
                    initial['market_id'] = criteria['market_id']
            # Pre-select "Schedule for later" with the step's suggested send time.
            scheduled_at = strategist_prefill.get('scheduled_at')
            if scheduled_at:
                initial['send_mode'] = SMSCampaignForm.SEND_SCHEDULE
                initial['scheduled_at'] = scheduled_at
        elif prefill:
            initial = {'name': f'SMS - {prefill["label"]}'}
        elif event:
            initial = {'name': event.name}
        else:
            initial = {}
        form = SMSCampaignForm(
            organization=org, event=event, initial=initial,
            has_manual_includes=has_manual,
        )

    # Events offered in the composer's "Add a ticket link" dropdown: live direct events
    # (buy page) plus imported events that have a third-party ticket link set. Narrow in
    # the DB, then finalize with _event_is_ticketable (effective_status is a property).
    ticket_link_events = [
        {'id': str(ev.id), 'name': ev.name, 'start_date': ev.start_date}
        for ev in Event.objects.filter(
            organization=org, deleted_at__isnull=True,
        ).filter(
            Q(ticketing_type=TICKETING_TYPE_DIRECT, status=EVENT_STATUS_LIVE)
            | (Q(ticketing_type=TICKETING_TYPE_EXTERNAL) & ~Q(ticket_link=''))
        ).order_by('-start_date')
        if _event_is_ticketable(ev)
    ]

    # Initial meter values match billing: segments are counted on the body plus
    # the auto-appended STOP footer (the JS meter mirrors this).
    encoding, segments = sms_segment_info(
        with_stop_footer(request.POST.get('body', '') if request.method == 'POST' else '')
    )
    # When the composer was opened from an AI plan step, carry the plan/step through
    # review→confirm (hidden fields) so a real send stamps that step launched + linked.
    if request.method == 'GET':
        prefill_plan_id = (strategist_prefill or {}).get('plan_id') or ''
        _prefill_step = (strategist_prefill or {}).get('step')
        prefill_step = '' if _prefill_step is None else str(_prefill_step)
    else:
        prefill_plan_id = request.POST.get('prefill_plan_id', '')
        prefill_step = request.POST.get('prefill_step', '')
    return render(request, 'tickets/marketing/sms/campaign_form.html', {
        'form': form,
        'audience_scope': audience_scope,
        'confirm_count': confirm_count,
        'exceeds_cap': exceeds_cap,
        'cap': cap,
        'preview_encoding': encoding,
        'preview_segments': segments,
        'event': event,
        'ticket_link_events': ticket_link_events,
        'confirm_cost_cents': confirm_cost_cents,
        'confirm_cost_tokens': confirm_cost_tokens,
        'balance_cents': org.sms_credit_balance_cents,
        'insufficient_credits': insufficient_credits,
        'idempotency_key': idem_key,
        'prefill': prefill,
        'prefill_plan_id': prefill_plan_id,
        'prefill_step': prefill_step,
        'manual_include_ids_csv': ','.join(manual_include_ids),
        'footer_disclosure_days': getattr(settings, 'SMS_FOOTER_DISCLOSURE_DAYS', 30),
    })


@login_required
@require_org
@require_host
@require_sms_feature
@require_POST
def sms_ticket_link(request):
    """JSON: get-or-create a shared 'SMS' tracking link for an event and return its
    absolute /t/<token>/ URL for insertion into a campaign body.

    Works for live direct events (redirects to the Cue buy page) and for imported
    events that have a third-party ticket link set (redirects off-site). Either way the
    link counts clicks. Uses SITE_URL so the stored link resolves for recipients rather
    than baking in the composer's host."""
    org = get_organization(request)
    event = get_object_or_404(
        Event.objects.filter(organization=org, deleted_at__isnull=True),
        id=request.POST.get('event'),
    )
    if not _event_is_ticketable(event):
        return JsonResponse(
            {'error': 'This event has no ticket link to send.'}, status=400,
        )
    url = _event_ticket_url(request, org, event)
    return JsonResponse({'url': url, 'name': event.name})


# A tracked ticket link inserted by the composer is a /t/<token>/ URL pointing
# at one of the org's TrackingLinks (see sms_ticket_link). We pull the token back out
# of the campaign body's link to attribute tickets + revenue on the detail page.
# Matches the legacy /track/ prefix too, so links sent before the rename still attribute.
_TRACK_TOKEN_RE = re.compile(r'/t(?:rack)?/([A-Za-z0-9]+)/')


def _mint_campaign_tracking_link(org, campaign):
    """Give this campaign its OWN tracking link for per-campaign attribution.

    The composer inserts a shared per-event 'SMS' link (/t/<token>/). At save we
    mint a fresh TrackingLink on the same event, named after the campaign, and rewrite
    the body to it — so each campaign's clicks/tickets/revenue attribute to it alone
    rather than pooling on the shared event link. Same-length token, so the segment
    count (and therefore the already-estimated charge) is unchanged. Mutates
    campaign.body and campaign.link_url in place; no-op when no ticket link is present.
    """
    match = _TRACK_TOKEN_RE.search(campaign.link_url or '')
    if not match:
        return
    src = TrackingLink.objects.filter(organization=org, token=match.group(1)).first()
    if not src:
        return
    new_link = TrackingLink.objects.create(
        organization=org, event=src.event,
        name=f'SMS · {campaign.name}'[:100], token=_generate_tracking_token(),
        target_url=src.target_url,  # preserve off-site redirect for external ticket links
    )
    # Replace the exact path as it appears in the body (handles both the
    # canonical /t/ and the legacy /track/ prefix).
    old_path = match.group(0)
    new_path = reverse('tickets:track_link_redirect', kwargs={'token': new_link.token})
    campaign.body = campaign.body.replace(old_path, new_path)
    campaign.link_url = campaign.link_url.replace(old_path, new_path)


def _sms_buy_stats(org, campaign):
    """Tickets bought + NET revenue (gross minus platform fee) attributed to the
    tracked ticket link in this campaign, from COMPLETED checkout sessions. Returns
    None when the campaign has no tracked ticket link. Mirrors the event tracking-
    link dashboard (views.py:4537): COMPLETED only, so refunded orders drop out."""
    match = _TRACK_TOKEN_RE.search(campaign.link_url or '')
    if not match:
        return None
    link = TrackingLink.objects.filter(organization=org, token=match.group(1)).first()
    if not link:
        return None
    completed = StripeCheckoutSession.objects.filter(
        tracking_link=link, status=StripeCheckoutSession.Status.COMPLETED,
        ticket_order__isnull=False,
    )
    rev_cents = completed.aggregate(
        v=Coalesce(Sum(F('amount_total_cents') - F('platform_fee_cents')), 0),
    )['v']
    tickets = Ticket.objects.filter(
        ticket_order_id__in=completed.values_list('ticket_order_id', flat=True),
    ).count()
    return {'tickets': tickets, 'revenue': Decimal(rev_cents) / 100, 'orders': completed.count()}


def _campaign_link_is_external(org, campaign):
    """True when the campaign's tracked ticket link redirects off-site (imported event).

    Cue can't see third-party sales, so orders/revenue aren't attributable for these —
    only clicks. Used to swap the buy-stat cards for an explanatory hint on the detail page.
    """
    match = _TRACK_TOKEN_RE.search(campaign.link_url or '')
    if not match:
        return False
    link = TrackingLink.objects.filter(organization=org, token=match.group(1)).first()
    return bool(link and link.target_url)


@login_required
@require_org
@require_host
@require_sms_feature
def sms_campaign_detail(request, pk):
    org = get_organization(request)
    campaign = get_object_or_404(
        _annotate_counts(
            SMSCampaign.objects.filter(organization=org, deleted_at__isnull=True)
            .select_related('event')
        ),
        id=pk,
    )
    recipients = (
        SMSMessageRecipient.objects.filter(campaign=campaign)
        .select_related('customer').order_by('-created_at')
    )
    paginator = Paginator(recipients, 50)
    page_obj = paginator.get_page(request.GET.get('page'))
    return render(request, 'tickets/marketing/sms/campaign_detail.html', {
        'campaign': campaign,
        'audience_summary': campaign.audience_summary(org),
        'buy_stats': _sms_buy_stats(org, campaign),
        'external_ticket_link': _campaign_link_is_external(org, campaign),
        'page_obj': page_obj,
        'link_events': _org_events_for_picker(org),
    })


@login_required
@require_org
@require_host
@require_sms_feature
@require_POST
def sms_campaign_cancel(request, pk):
    org = get_organization(request)
    # Only a still-scheduled campaign can be canceled; atomic so it can't race a send.
    updated = SMSCampaign.objects.filter(
        id=pk, organization=org, status=SMSCampaign.Status.SCHEDULED,
    ).update(status=SMSCampaign.Status.CANCELED)
    if updated:
        # Refund the credits reserved at scheduling time (idempotent).
        from .services.sms_credits import refund_campaign
        campaign = SMSCampaign.objects.get(id=pk)
        refunded = refund_campaign(campaign, description='Scheduled campaign canceled')
        if refunded:
            messages.success(
                request,
                f'Scheduled campaign canceled. ${refunded / 100:.2f} in credits refunded.',
            )
        else:
            messages.success(request, 'Scheduled campaign canceled.')
    else:
        messages.error(request, 'That campaign can no longer be canceled.')
    return redirect('tickets:sms_campaign_detail', pk=pk)


@login_required
@require_org
@require_host
@require_sms_feature
@require_POST
def sms_campaign_link_event(request, pk):
    """Associate a campaign with an event (or clear it). Works for any status —
    organizers commonly link an already-sent campaign after the fact so the
    attribution shows up on both the campaign and the event."""
    org = get_organization(request)
    campaign = get_object_or_404(
        SMSCampaign.objects.filter(organization=org, deleted_at__isnull=True), id=pk,
    )
    event_id = (request.POST.get('event') or '').strip()
    if event_id:
        event = get_object_or_404(
            Event.objects.filter(organization=org, deleted_at__isnull=True), id=event_id,
        )
        campaign.event = event
        msg = f'Linked "{campaign.name}" to {event.name}.'
    else:
        campaign.event = None
        msg = f'Unlinked "{campaign.name}" from its event.'
    campaign.updated_by = request.user
    campaign.save(update_fields=['event', 'updated_by', 'updated_at'])

    # Event/overview rollups read this linkage, so bust their caches.
    from .views import _invalidate_marketing_cache
    _invalidate_marketing_cache(org)

    messages.success(request, msg)

    next_url = request.POST.get('next')
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()},
    ):
        return redirect(next_url)
    return redirect('tickets:sms_campaign_list')


# ---------------------------------------------------------------------------
# Twilio webhooks (public, signature-validated)
# ---------------------------------------------------------------------------

# Twilio's 'queued' is intentionally absent: a message we track by twilio_sid has
# already been handed off, so a 'queued' callback is always stale. Mapping it to
# Status.QUEUED used to regress an already-sent row back into the send queue, which
# the recovery cron then re-sent (the double-text bug). Unmapped statuses no-op.
_TWILIO_STATUS_MAP = {
    'sending': SMSMessageRecipient.Status.SENT,
    'sent': SMSMessageRecipient.Status.SENT,
    'delivered': SMSMessageRecipient.Status.DELIVERED,
    'undelivered': SMSMessageRecipient.Status.UNDELIVERED,
    'failed': SMSMessageRecipient.Status.FAILED,
}


@csrf_exempt
@require_POST
def twilio_sms_status_webhook(request):
    """Per-message delivery status callback. Idempotent — Twilio retries these."""
    if not validate_twilio_request(request):
        return HttpResponse(status=403)
    sid = request.POST.get('MessageSid', '')
    raw_status = (request.POST.get('MessageStatus') or '').lower()
    new_status = _TWILIO_STATUS_MAP.get(raw_status)
    recipient = SMSMessageRecipient.objects.filter(twilio_sid=sid).first()
    if not recipient or not new_status:
        return HttpResponse(status=200)  # unknown sid / status → no-op, never 500

    # Callbacks are unordered and retried, so a transient 'sent'/'sending' can land
    # after a terminal one. Only let SENT advance a still-unsent (QUEUED) row; never
    # pull a handed-off or terminal row backward. Terminal-vs-terminal stays
    # last-write-wins — we don't store event timestamps to order them.
    if (new_status == SMSMessageRecipient.Status.SENT
            and recipient.status != SMSMessageRecipient.Status.QUEUED):
        return HttpResponse(status=200)

    recipient.status = new_status
    if new_status == SMSMessageRecipient.Status.DELIVERED and not recipient.delivered_at:
        recipient.delivered_at = timezone.now()
    if new_status in (SMSMessageRecipient.Status.FAILED, SMSMessageRecipient.Status.UNDELIVERED):
        recipient.error_code = request.POST.get('ErrorCode', '') or recipient.error_code
        recipient.error_message = request.POST.get('ErrorMessage', '') or recipient.error_message
        # A carrier/Twilio opt-out block (21610) arriving via callback rather than the
        # inbound OptOutType webhook: mirror it into suppression so we never re-attempt.
        if recipient.error_code in TWILIO_OPT_OUT_ERROR_CODES and recipient.phone:
            PhoneSuppression.objects.get_or_create(
                phone=recipient.phone, organization=None,
                defaults={'reason': PhoneSuppression.Reason.TWILIO_STOP},
            )
    recipient.save(update_fields=[
        'status', 'delivered_at', 'error_code', 'error_message', 'updated_at',
    ])
    return HttpResponse(status=200)


@csrf_exempt
@require_POST
def twilio_sms_inbound_webhook(request):
    """Inbound message webhook. We only mirror Twilio's opt-out classification
    (OptOutType) into PhoneSuppression — Twilio itself enforces STOP/HELP."""
    if not validate_twilio_request(request):
        return HttpResponse(status=403)
    from_phone = normalize_phone(request.POST.get('From', ''))
    opt_out_type = (request.POST.get('OptOutType') or '').upper()
    if from_phone and opt_out_type == 'STOP':
        PhoneSuppression.objects.get_or_create(
            phone=from_phone, organization=None,
            defaults={'reason': PhoneSuppression.Reason.TWILIO_STOP},
        )
        # Attribute the opt-out to the campaign that most recently texted this phone.
        recent = (
            SMSMessageRecipient.objects
            .filter(phone=from_phone, status__in=['sent', 'delivered', 'undelivered'],
                    opted_out_at__isnull=True)
            .order_by('-sent_at', '-created_at')
            .first()
        )
        if recent:
            recent.opted_out_at = timezone.now()
            recent.save(update_fields=['opted_out_at', 'updated_at'])
    elif from_phone and opt_out_type == 'START':
        PhoneSuppression.objects.filter(phone=from_phone, organization__isnull=True).delete()
        # A subscriber who consented while globally STOP'd is now reachable —
        # clear the pending_start lifecycle flag so the org's audience reflects it.
        SMSConsentRecord.objects.filter(
            phone=from_phone, pending_start=True,
        ).update(pending_start=False)
    # HELP / normal inbound → no-op (Twilio replies). Empty TwiML.
    return HttpResponse('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                        content_type='text/xml')


def sms_click_redirect(request, token):
    """Public: record a click on a tracked SMS link, then 302 to the target.

    Mirrors track_link_redirect — no auth (recipients click from their phones).
    Counts are atomic (F()/Coalesce) so concurrent taps don't lose updates."""
    recipient = get_object_or_404(
        SMSMessageRecipient.objects.select_related('campaign'), click_token=token,
    )
    target = recipient.campaign.link_url
    if not target:
        raise Http404('No link target for this campaign.')
    SMSMessageRecipient.objects.filter(pk=recipient.pk).update(
        click_count=F('click_count') + 1,
        first_clicked_at=Coalesce(F('first_clicked_at'), Now()),
    )
    return redirect(target)


# ---------------------------------------------------------------------------
# Prepaid SMS credit wallet (Stripe Checkout top-ups)
# ---------------------------------------------------------------------------

# Token packs (1 token = 1 SMS segment). The dollar price is derived from the
# per-segment price, so a pack always lands the balance on a whole-token amount.
SMS_CREDIT_PRESETS_TOKENS = [500, 1000, 2500, 5000]


def _org_stripe_customer_id(org):
    """Return the org's platform billing Stripe Customer id, creating it lazily.

    Non-fatal: on any Stripe error we log and return None so a plain (no-saved-card)
    Checkout still works. Mirrors the per-user pattern in views.create_payment_intent,
    org-scoped. Stores the id on the Organization so it's reused for the next card.
    """
    if org.stripe_customer_id:
        return org.stripe_customer_id
    import stripe as stripe_lib
    from .models import Organization
    stripe_lib.api_key = settings.STRIPE_SECRET_KEY
    try:
        customer = stripe_lib.Customer.create(
            name=org.name, metadata={'organization_id': str(org.id)},
        )
    except Exception:
        logger.exception('Stripe Customer create failed for org %s', org.id)
        return None
    Organization.objects.filter(pk=org.pk).update(stripe_customer_id=customer.id)
    org.stripe_customer_id = customer.id
    return customer.id


@login_required
@require_org
@require_host
@require_sms_feature
def sms_credits(request):
    """Wallet page: token balance, token packs, and the ledger."""
    from .models import SMSCreditTransaction
    from .services.sms_credits import price_per_segment_cents
    org = get_organization(request)
    price = price_per_segment_cents()
    presets = [{'tokens': t, 'cents': int(t * price)} for t in SMS_CREDIT_PRESETS_TOKENS]
    transactions = (
        SMSCreditTransaction.objects.filter(organization=org)
        .select_related('campaign').order_by('-created_at')[:30]
    )
    has_saved_card = bool(org.stripe_pm_id and org.stripe_customer_id)
    card_exp = ''
    if org.stripe_pm_exp_month and org.stripe_pm_exp_year:
        card_exp = f'{org.stripe_pm_exp_month:02d}/{org.stripe_pm_exp_year}'
    return render(request, 'tickets/marketing/sms/credits.html', {
        'balance_cents': org.sms_credit_balance_cents,
        'presets': presets,
        'transactions': transactions,
        'stripe_ready': bool(getattr(settings, 'STRIPE_SECRET_KEY', '')),
        'marketing_section': 'sms',
        'has_saved_card': has_saved_card,
        'card_brand': org.stripe_pm_brand,
        'card_last4': org.stripe_pm_last4,
        'card_exp': card_exp,
    })


@login_required
@require_org
@require_host
@require_sms_feature
@require_POST
def sms_credits_checkout(request):
    """Create a Stripe Checkout Session for a credit top-up (org pays the platform
    directly) and redirect the buyer to Stripe's hosted page."""
    org = get_organization(request)
    from .services.sms_credits import price_per_segment_cents
    try:
        token_pack = int(request.POST.get('tokens', '0'))
    except (TypeError, ValueError):
        token_pack = 0
    if token_pack not in SMS_CREDIT_PRESETS_TOKENS:
        messages.error(request, 'Pick a valid token pack.')
        return redirect('tickets:sms_credits')
    amount_cents = int(token_pack * price_per_segment_cents())
    if not getattr(settings, 'STRIPE_SECRET_KEY', ''):
        messages.error(request, 'Payments are not configured for this environment.')
        return redirect('tickets:sms_credits')

    import stripe as stripe_lib
    stripe_lib.api_key = settings.STRIPE_SECRET_KEY
    success_url = request.build_absolute_uri(
        reverse('tickets:sms_credits_success')
    ) + '?session_id={CHECKOUT_SESSION_ID}'
    cancel_url = request.build_absolute_uri(reverse('tickets:sms_credits'))
    session_kwargs = dict(
        mode='payment',
        line_items=[{
            'price_data': {
                'currency': getattr(settings, 'STRIPE_CURRENCY', 'usd'),
                'product_data': {'name': f'{token_pack} marketing SMS tokens'},
                'unit_amount': amount_cents,
            },
            'quantity': 1,
        }],
        metadata={
            'kind': 'sms_credits',
            'organization_id': str(org.id),
            'credit_cents': str(amount_cents),
        },
        success_url=success_url,
        cancel_url=cancel_url,
    )
    # "Save this card" → attach an org Customer and tell Stripe to keep the card for
    # off-session reuse. Crediting still happens via checkout.session.completed; the
    # card brand/last4 are saved by the payment_intent.succeeded handler (flow=checkout).
    if request.POST.get('save_card') == '1':
        customer_id = _org_stripe_customer_id(org)
        if customer_id:
            session_kwargs['customer'] = customer_id
            session_kwargs['payment_intent_data'] = {
                'setup_future_usage': 'off_session',
                'metadata': {
                    'kind': 'sms_credits',
                    'organization_id': str(org.id),
                    'credit_cents': str(amount_cents),
                    'flow': 'checkout',
                },
            }
    try:
        session = stripe_lib.checkout.Session.create(**session_kwargs)
    except Exception:
        logger.exception('Stripe checkout session create failed for org %s', org.id)
        messages.error(request, 'Could not start checkout. Please try again.')
        return redirect('tickets:sms_credits')
    return redirect(session.url)


@login_required
@require_org
@require_host
@require_sms_feature
def sms_credits_success(request):
    """Stripe success landing. Fulfills the credit immediately (idempotent) so it
    lands even when no webhook is configured (e.g. local dev)."""
    org = get_organization(request)
    session_id = request.GET.get('session_id', '')
    if session_id and getattr(settings, 'STRIPE_SECRET_KEY', ''):
        try:
            import stripe as stripe_lib
            from .views import _fulfill_sms_credit_checkout
            stripe_lib.api_key = settings.STRIPE_SECRET_KEY
            session = stripe_lib.checkout.Session.retrieve(session_id)
            _fulfill_sms_credit_checkout(session)
        except Exception:
            logger.exception('Stripe success fulfillment failed for %s', session_id)
    messages.success(request, 'Payment received — your SMS tokens have been added.')
    return redirect('tickets:sms_credits')


@login_required
@require_org
@require_host
@require_sms_feature
@require_POST
def sms_credits_charge_saved(request):
    """One-click top-up: charge the org's saved card off-session and credit the wallet.

    Credits synchronously on success (idempotent via the PaymentIntent id); the
    payment_intent.succeeded webhook is a no-op retry under the same id. On any card
    problem we fall back to the hosted Checkout flow rather than failing hard.
    """
    org = get_organization(request)
    from .services.sms_credits import price_per_segment_cents, credit
    from .models import Organization
    try:
        token_pack = int(request.POST.get('tokens', '0'))
    except (TypeError, ValueError):
        token_pack = 0
    if token_pack not in SMS_CREDIT_PRESETS_TOKENS:
        messages.error(request, 'Pick a valid token pack.')
        return redirect('tickets:sms_credits')
    if not getattr(settings, 'STRIPE_SECRET_KEY', ''):
        messages.error(request, 'Payments are not configured for this environment.')
        return redirect('tickets:sms_credits')
    if not (org.stripe_pm_id and org.stripe_customer_id):
        messages.error(request, 'No saved card on file. Add one by topping up below.')
        return redirect('tickets:sms_credits')

    amount_cents = int(token_pack * price_per_segment_cents())
    import stripe as stripe_lib
    stripe_lib.api_key = settings.STRIPE_SECRET_KEY
    try:
        pi = stripe_lib.PaymentIntent.create(
            amount=amount_cents,
            currency=getattr(settings, 'STRIPE_CURRENCY', 'usd'),
            customer=org.stripe_customer_id,
            payment_method=org.stripe_pm_id,
            off_session=True,
            confirm=True,
            metadata={
                'kind': 'sms_credits',
                'organization_id': str(org.id),
                'credit_cents': str(amount_cents),
                'flow': 'one_click',
            },
        )
    except stripe_lib.error.CardError as e:
        if getattr(e, 'code', '') == 'authentication_required':
            messages.error(request, 'Your card needs verification. Please top up below to '
                                    'confirm it once, then one-click will work again.')
        else:
            messages.error(request, 'Your saved card was declined. Try another card below.')
        return redirect('tickets:sms_credits')
    except stripe_lib.error.InvalidRequestError as e:
        if getattr(e, 'code', '') == 'resource_missing':
            # The saved PM (or customer) no longer exists at Stripe — clear and fall back.
            fields = {'stripe_pm_id': None, 'stripe_pm_brand': '', 'stripe_pm_last4': '',
                      'stripe_pm_exp_month': None, 'stripe_pm_exp_year': None}
            if getattr(e, 'param', '') == 'customer':
                fields['stripe_customer_id'] = None
            Organization.objects.filter(pk=org.pk).update(**fields)
            messages.error(request, 'Your saved card is no longer available. Please add a card '
                                    'by topping up below.')
            return redirect('tickets:sms_credits')
        logger.exception('Stripe one-click PaymentIntent failed for org %s', org.id)
        messages.error(request, 'Could not charge your saved card. Please try again.')
        return redirect('tickets:sms_credits')
    except Exception:
        logger.exception('Stripe one-click PaymentIntent failed for org %s', org.id)
        messages.error(request, 'Could not charge your saved card. Please try again.')
        return redirect('tickets:sms_credits')

    if getattr(pi, 'status', '') != 'succeeded':
        # e.g. requires_action — hosted Checkout handles SCA.
        messages.error(request, 'Your card needs verification. Please top up below to confirm it.')
        return redirect('tickets:sms_credits')

    credit(str(org.id), getattr(pi, 'amount_received', None) or amount_cents,
           stripe_checkout_session_id=pi.id, description='Stripe one-click top-up')
    messages.success(request, 'Payment received — your SMS tokens have been added.')
    return redirect('tickets:sms_credits')


@login_required
@require_org
@require_host
@require_sms_feature
@require_POST
def sms_credits_remove_card(request):
    """Detach the org's saved card at Stripe and clear the local card fields.

    Keeps stripe_customer_id so the next saved card reuses the same Customer."""
    org = get_organization(request)
    from .models import Organization
    if org.stripe_pm_id and getattr(settings, 'STRIPE_SECRET_KEY', ''):
        import stripe as stripe_lib
        stripe_lib.api_key = settings.STRIPE_SECRET_KEY
        try:
            stripe_lib.PaymentMethod.detach(org.stripe_pm_id)
        except Exception:
            # Already detached/missing — still clear locally below.
            logger.warning('Detach failed for PM %s (org %s); clearing anyway',
                           org.stripe_pm_id, org.id)
    Organization.objects.filter(pk=org.pk).update(
        stripe_pm_id=None, stripe_pm_brand='', stripe_pm_last4='',
        stripe_pm_exp_month=None, stripe_pm_exp_year=None,
    )
    messages.success(request, 'Saved card removed.')
    return redirect('tickets:sms_credits')


# ---------------------------------------------------------------------------
# AI Campaign Strategist (plan recommendations)
# ---------------------------------------------------------------------------

# Purpose -> display label + Bootstrap color for the plan timeline badges.
PLAN_PURPOSE_LABELS = {
    'announcement': ('Announcement', 'primary'),
    'early_bird': ('Early bird', 'info'),
    'social_proof': ('Social proof', 'success'),
    'reminder': ('Reminder', 'secondary'),
    'last_chance': ('Last chance', 'danger'),
    'thank_you': ('Thank you', 'success'),
    're_engagement': ('Re-engagement', 'warning'),
}


def _plan_criteria_from_post(post):
    """Segment audience for a plan (no event-scope collapse — event is separate)."""
    criteria = {}
    segments = [s for s in post.getlist('rfm_segment') if s]
    if segments:
        criteria['rfm_segment'] = segments
    tag_ids = _valid_uuids([t for t in post.getlist('tag_ids') if t])
    if tag_ids:
        criteria['tag_ids'] = tag_ids
    market_id = (post.get('market_id') or '').strip()
    if market_id:
        criteria['market_id'] = market_id
    return criteria


def _audience_option_lists(org):
    """The org's audience building blocks (segments/tags/markets) for pickers."""
    markets, has_no_market = market_filter_options(org)
    market_choices = [(str(m.id), m.name) for m in markets]
    if has_no_market:
        market_choices.append((NO_MARKET_VALUE, 'No market'))
    return {
        'segment_choices': [c[0] for c in SMS_SEGMENT_CHOICES],
        'tags': list(CustomerTag.objects.filter(organization=org).order_by('name')),
        'market_choices': market_choices,
    }


def _plan_form_context(org, event=None, selected_criteria=None):
    """Shared context for the plan generate form (segments/tags/markets/events)."""
    opts = _audience_option_lists(org)
    market_choices = opts['market_choices']
    # Recent + upcoming events the organizer might plan for.
    events = list(
        Event.objects.filter(organization=org, deleted_at__isnull=True)
        .select_related('venue').order_by('-start_date')[:100]
    )
    sel = selected_criteria or {}
    return {
        'event': event,
        'plan_events': events,
        'segment_choices': opts['segment_choices'],
        'selected_segments': sel.get('rfm_segment') or [],
        'tags': opts['tags'],
        'selected_tag_ids': [str(t) for t in (sel.get('tag_ids') or [])],
        'market_choices': market_choices,
        'selected_market_id': sel.get('market_id') or '',
    }


def _tracking_link_absolute_url(request, link):
    """Absolute /t/<token>/ URL, using SITE_URL so it resolves for recipients off-site."""
    path = reverse('tickets:track_link_redirect', kwargs={'token': link.token})
    site_url = getattr(settings, 'SITE_URL', '').rstrip('/')
    return f"{site_url}{path}" if site_url else request.build_absolute_uri(path)


def _event_is_ticketable(event):
    """True when an event can offer a tracked SMS ticket link: a live direct event, or an
    imported/external event that has a third-party ticket page set."""
    if event is None:
        return False
    if event.ticketing_type == TICKETING_TYPE_DIRECT:
        return event.effective_status == EVENT_STATUS_LIVE
    return event.ticketing_type == TICKETING_TYPE_EXTERNAL and bool(event.ticket_link)


def _event_ticket_url(request, org, event):
    """Best-effort tracked ticket URL for an event, or '' when none applies.

    Direct+live events point at the Cue buy page; external events with a ticket_link get a
    short /t/ link that redirects off-site to that page (both track clicks)."""
    if not _event_is_ticketable(event):
        return ''
    external = event.ticketing_type == TICKETING_TYPE_EXTERNAL
    link, _ = TrackingLink.objects.get_or_create(
        organization=org, event=event, name='SMS',
        defaults={
            'token': _generate_tracking_token(),
            'target_url': event.ticket_link if external else '',
        },
    )
    # Keep the target current if the organizer later edited the event's ticket link.
    if external and link.target_url != event.ticket_link:
        link.target_url = event.ticket_link
        link.save(update_fields=['target_url'])
    return _tracking_link_absolute_url(request, link)


@login_required
@require_org
@require_host
@require_sms_feature
@require_http_methods(['GET', 'POST'])
def sms_plan_create(request):
    """Generate an AI campaign plan for an event or a customer segment.

    GET renders the generate form; POST calls the strategist, persists an
    SMSCampaignPlan, and redirects to its detail page.
    """
    from django.core.cache import cache as django_cache

    org = get_organization(request)
    if not org.ai_sms_strategist_enabled:
        raise Http404()

    event = None
    event_id = request.POST.get('event') or request.GET.get('event')
    if event_id:
        event = get_object_or_404(
            Event.objects.filter(organization=org, deleted_at__isnull=True)
            .select_related('venue'),
            id=event_id,
        )

    if request.method == 'POST':
        objective = (request.POST.get('objective') or '').strip()[:300]
        criteria = {} if event is not None else _plan_criteria_from_post(request.POST)

        if event is None and not criteria:
            messages.error(request, 'Pick an event, or choose at least one segment, tag, or market.')
            return render(request, 'tickets/marketing/sms/plan_form.html',
                          _plan_form_context(org, event, criteria))

        # Rate limit: 20 successful generations per org per hour (ceiling check only).
        rate_key = f"sms_plan_ratelimit:{org.id}"
        try:
            if (django_cache.get(rate_key, 0) or 0) >= 20:
                messages.error(request, 'Too many plans generated in the last hour. Please try again later.')
                return render(request, 'tickets/marketing/sms/plan_form.html',
                              _plan_form_context(org, event, criteria))
        except Exception:
            pass

        from .services.sms_strategist import generate_campaign_plan, SMSStrategistError
        ticket_url = _event_ticket_url(request, org, event) if event is not None else ''
        try:
            result = generate_campaign_plan(
                org, event=event, criteria=criteria or None,
                objective=objective, ticket_url=ticket_url, user=request.user,
            )
        except SMSStrategistError as exc:
            messages.error(request, str(exc))
            return render(request, 'tickets/marketing/sms/plan_form.html',
                          _plan_form_context(org, event, criteria))

        if event is not None:
            name = f'Plan · {event.name}'
        else:
            tmp = SMSCampaign(organization=org, filter_criteria=criteria)
            name = f'Plan · {tmp.audience_summary(org)}'

        plan = SMSCampaignPlan.objects.create(
            organization=org, created_by=request.user, event=event,
            filter_criteria=criteria, name=name[:200], objective=objective,
            strategy_summary=result['strategy_summary'], model_name=result['model_name'],
            steps=result['steps'], status=SMSCampaignPlan.Status.DRAFT,
        )

        # Count only successful generations against the hourly budget.
        try:
            current = django_cache.get(rate_key, 0) or 0
            django_cache.set(rate_key, current + 1, timeout=3600)
        except Exception:
            pass

        return redirect('tickets:sms_plan_detail', pk=plan.id)

    # GET: optionally prefill a segment audience from query params (e.g. from the
    # segments page: ?segment=VIP&market=<id>).
    selected = None
    if event is None:
        seg_criteria = {}
        segs = [s for s in request.GET.getlist('segment') if s]
        if segs:
            seg_criteria['rfm_segment'] = segs
        market = (request.GET.get('market') or '').strip()
        if market:
            seg_criteria['market_id'] = market
        selected = seg_criteria or None
    return render(request, 'tickets/marketing/sms/plan_form.html',
                  _plan_form_context(org, event, selected))


def _decorate_plan_steps(steps, tz):
    """Attach display labels/colors + org-local send time to each step for the template."""
    out = []
    for step in steps or []:
        label, color = PLAN_PURPOSE_LABELS.get(
            step.get('purpose'), (step.get('purpose', 'Message').replace('_', ' ').title(), 'secondary'),
        )
        # Org-local "YYYY-MM-DDTHH:MM" for the datetime-local editor (empty for legacy
        # plans that predate structured scheduling).
        send_local = ''
        raw = step.get('send_at')
        if raw:
            try:
                send_local = datetime.fromisoformat(raw).astimezone(tz).strftime('%Y-%m-%dT%H:%M')
            except (ValueError, TypeError):
                send_local = ''
        out.append({**step, 'purpose_label': label, 'purpose_color': color,
                    'send_local': send_local,
                    'audience_criteria_json': json.dumps(step.get('audience_criteria') or {})})
    return out


# Launched-campaign status → (pill label, extra CSS class, icon) so a launched step
# reflects its campaign's real state (e.g. "Scheduled" for a future send) rather than a
# generic "Launched". Falls back to "Launched" for an unknown/deleted campaign.
_LAUNCHED_PILL = {
    'scheduled': ('Scheduled', 'launched-pill--scheduled', 'bi-clock'),
    'sending':   ('Sending',   '',                         'bi-arrow-repeat'),
    'sent':      ('Sent',      '',                         'bi-check2'),
    'canceled':  ('Canceled',  'launched-pill--muted',     'bi-x-circle'),
    'failed':    ('Failed',    'launched-pill--muted',     'bi-exclamation-circle'),
}


def _annotate_launched_status(org, steps):
    """Attach an accurate launched pill (label/class/icon) to each launched step by
    resolving its campaign's current status in a single query."""
    ids = [s['launched_campaign_id'] for s in steps if s.get('launched_campaign_id')]
    status_by_id = {}
    if ids:
        status_by_id = {
            str(cid): status for cid, status in
            SMSCampaign.objects.filter(organization=org, id__in=ids).values_list('id', 'status')
        }
    for s in steps:
        cid = s.get('launched_campaign_id')
        if not cid:
            continue
        label, css, icon = _LAUNCHED_PILL.get(
            status_by_id.get(str(cid)), ('Launched', '', 'bi-check2'),
        )
        s['launched_label'] = label
        s['launched_pill_class'] = css
        s['launched_icon'] = icon


def _audience_label_for(org, criteria):
    """Human label for an audience criteria dict (used after an inline audience edit).

    Delegates to the strategist helper so the label wording matches the composer.
    """
    from .services.sms_strategist import plan_audience_label
    return plan_audience_label(org, criteria)


@login_required
@require_org
@require_host
@require_sms_feature
def sms_plan_detail(request, pk):
    """Render a saved plan: strategy summary + the sequence timeline."""
    org = get_organization(request)
    if not org.ai_sms_strategist_enabled:
        raise Http404()
    plan = get_object_or_404(
        SMSCampaignPlan.objects.filter(organization=org).select_related('event'),
        id=pk,
    )
    steps = _decorate_plan_steps(plan.steps, org.get_timezone())
    _annotate_launched_status(org, steps)
    context = {'plan': plan, 'steps': steps}
    context.update(_audience_option_lists(org))
    return render(request, 'tickets/marketing/sms/plan_detail.html', context)


@login_required
@require_org
@require_host
@require_sms_feature
def sms_plan_list(request):
    """List the org's past AI campaign plans, optionally filtered by status."""
    org = get_organization(request)
    if not org.ai_sms_strategist_enabled:
        raise Http404()
    plans = (
        SMSCampaignPlan.objects.filter(organization=org)
        .select_related('event').order_by('-created_at')
    )
    status_filter = request.GET.get('status', '')
    if status_filter in SMSCampaignPlan.Status.values:
        plans = plans.filter(status=status_filter)
    else:
        status_filter = ''
    page = Paginator(plans, 25).get_page(request.GET.get('page'))
    return render(request, 'tickets/marketing/sms/plan_list.html', {
        'page_obj': page,
        'status_filter': status_filter,
    })


@login_required
@require_org
@require_host
@require_sms_feature
@require_POST
def sms_plan_delete(request, pk):
    """Discard a whole plan. The plan is advisory, so this is a hard delete; any campaigns
    already launched from its steps are separate SMSCampaign rows and are left untouched."""
    org = get_organization(request)
    if not org.ai_sms_strategist_enabled:
        raise Http404()
    plan = get_object_or_404(SMSCampaignPlan.objects.filter(organization=org), id=pk)
    plan.delete()
    messages.success(request, 'Plan deleted.')
    return redirect('tickets:sms_plan_list')


def _apply_step_body(step_dict, body):
    """Return a copy of a plan step with a new body + recomputed segment count/encoding.

    Segments/encoding mirror the composer meter: counted on the body plus the
    auto-appended STOP footer (worst case), so the number shown matches billing.
    """
    body = (body or '')[:1600]
    encoding, segments = sms_segment_info(with_stop_footer(body))
    return {**step_dict, 'body': body, 'segments': segments, 'encoding': encoding}


@login_required
@require_org
@require_host
@require_sms_feature
@require_POST
def sms_plan_update_step(request, pk, step):
    """Persist an inline edit to one plan step's message; return the new segment info.

    JSON endpoint used by the plan detail page as the organizer edits a message in
    place, so the edit survives a refresh and the segment/token count stays truthful.
    """
    org = get_organization(request)
    if not org.ai_sms_strategist_enabled:
        raise Http404()
    plan = get_object_or_404(SMSCampaignPlan.objects.filter(organization=org), id=pk)

    steps = plan.steps or []
    if step < 0 or step >= len(steps):
        raise Http404()

    body = (request.POST.get('body') or '').strip()
    if not body:
        return JsonResponse({'ok': False, 'error': 'Message cannot be empty.'}, status=400)

    steps[step] = _apply_step_body(steps[step], body)
    plan.steps = steps
    plan.save(update_fields=['steps', 'updated_at'])
    return JsonResponse({
        'ok': True,
        'segments': steps[step]['segments'],
        'encoding': steps[step]['encoding'],
    })


@login_required
@require_org
@require_host
@require_sms_feature
@require_POST
def sms_plan_update_audience(request, pk, step):
    """Change which subscribers one plan step targets; return the new audience label.

    ``audience_mode`` is 'event' (event plans → the event's attendees) or 'custom'
    (a segment/tag/market selection, which must be non-empty).
    """
    org = get_organization(request)
    if not org.ai_sms_strategist_enabled:
        raise Http404()
    plan = get_object_or_404(SMSCampaignPlan.objects.filter(organization=org), id=pk)

    steps = plan.steps or []
    if step < 0 or step >= len(steps):
        raise Http404()

    mode = request.POST.get('audience_mode') or 'custom'
    if mode == 'all' and plan.event_id:
        criteria = {'all_subscribers': True}
    elif mode == 'event' and plan.event_id:
        criteria = {'event_id': str(plan.event_id)}
    else:
        criteria = _plan_criteria_from_post(request.POST)
        if not criteria:
            return JsonResponse(
                {'ok': False, 'error': 'Pick at least one segment, tag, or market.'},
                status=400,
            )

    label = _audience_label_for(org, criteria)
    steps[step] = {**steps[step], 'audience_criteria': criteria, 'audience_label': label}
    plan.steps = steps
    plan.save(update_fields=['steps', 'updated_at'])
    return JsonResponse({'ok': True, 'audience_label': label})


@login_required
@require_org
@require_host
@require_sms_feature
@require_POST
def sms_plan_update_schedule(request, pk, step):
    """Persist an edited send date/time for one plan step; return the new label.

    Accepts a `send_at` datetime-local value ("YYYY-MM-DDTHH:MM", org-local). Stores the
    aware datetime, recomputes the display label (with timezone) and the offset/time so
    the step stays self-consistent for later launches.
    """
    from tickets.services.sms_strategist import format_send_label

    org = get_organization(request)
    if not org.ai_sms_strategist_enabled:
        raise Http404()
    plan = get_object_or_404(SMSCampaignPlan.objects.filter(organization=org), id=pk)

    steps = plan.steps or []
    if step < 0 or step >= len(steps):
        raise Http404()

    raw = (request.POST.get('send_at') or '').strip()
    try:
        naive = datetime.strptime(raw, '%Y-%m-%dT%H:%M')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Enter a valid date and time.'}, status=400)

    org_tz = org.get_timezone()
    dt = naive.replace(tzinfo=org_tz)
    # Keep offset_days in sync with the chosen date (informational; anchored on the
    # event date for event plans, else on today in the org's timezone).
    if plan.event_id and plan.event and plan.event.start_date:
        offset_days = max(0, (plan.event.start_date - dt.date()).days)
    else:
        offset_days = max(0, (dt.date() - timezone.now().astimezone(org_tz).date()).days)

    steps[step] = {
        **steps[step],
        'send_at': dt.isoformat(),
        'send_time': dt.strftime('%H:%M'),
        'offset_days': offset_days,
        'timing_label': format_send_label(dt),
    }
    plan.steps = steps
    plan.save(update_fields=['steps', 'updated_at'])
    return JsonResponse({
        'ok': True,
        'timing_label': steps[step]['timing_label'],
        'send_local': dt.strftime('%Y-%m-%dT%H:%M'),
    })


@login_required
@require_org
@require_host
@require_sms_feature
@require_POST
def sms_plan_launch_step(request, pk, step):
    """Open one plan step in the full composer (prefilled body + audience + schedule).

    This is the "Open in full editor" escape hatch. It does NOT mark the step launched —
    merely opening the editor isn't a send. The origin plan/step is carried in the prefill
    so the composer stamps this step launched only when it actually creates the campaign.
    """
    org = get_organization(request)
    if not org.ai_sms_strategist_enabled:
        raise Http404()
    plan = get_object_or_404(SMSCampaignPlan.objects.filter(organization=org), id=pk)

    steps = plan.steps or []
    if step < 0 or step >= len(steps):
        raise Http404()
    target = steps[step]

    # An edit typed into the message box (and not yet blur-saved) is authoritative:
    # apply + persist it so what launches is exactly what the organizer sees.
    body_changed = False
    override_body = request.POST.get('body')
    if override_body is not None and override_body.strip():
        target = _apply_step_body(target, override_body.strip())
        steps[step] = target
        body_changed = True

    criteria = target.get('audience_criteria') or {}
    # Map the step's audience to the composer entry point. Event-mode scopes let us keep
    # the campaign linked to the event while targeting ticket buyers ('event') or the
    # whole list ('all'); a segment/tag/market audience opens the plain (non-event)
    # composer. Driven off the STEP's own criteria so an edited audience is honored.
    plan_event_id = str(plan.event_id) if plan.event_id else None
    event_id = None
    audience_scope = None
    if criteria.get('event_id'):
        event_id = criteria['event_id']
        audience_scope = 'event'
    elif criteria.get('all_subscribers') and plan_event_id:
        event_id = plan_event_id
        audience_scope = 'all'
    # Carry the step's suggested send time into the composer's schedule field — but
    # only if it's still in the future (a past suggestion would fail the composer's
    # "must be in the future" check), formatted in the org's timezone.
    scheduled_local = ''
    raw_send_at = target.get('send_at')
    if raw_send_at:
        try:
            send_dt = datetime.fromisoformat(raw_send_at)
            if send_dt > timezone.now():
                scheduled_local = send_dt.astimezone(org.get_timezone()).strftime('%Y-%m-%dT%H:%M')
        except (ValueError, TypeError):
            scheduled_local = ''
    # Prefill payload consumed by sms_campaign_create on the next GET. plan_id/step let
    # the composer stamp this step launched + linked once it actually sends.
    request.session['sms_compose_prefill'] = {
        'body': target.get('body', ''),
        'criteria': criteria,
        'event_id': event_id,
        'scheduled_at': scheduled_local,
        'name': f"{plan.name} · {target.get('purpose_label') or target.get('purpose') or 'Message'}"[:200],
        'plan_id': str(plan.id),
        'step': step,
    }
    request.session.modified = True

    # Persist a just-typed body edit so the composer prefill matches, but do NOT mark
    # launched here — that happens only on an actual send.
    if body_changed:
        plan.steps = steps
        plan.save(update_fields=['steps', 'updated_at'])

    base = reverse('tickets:sms_campaign_create')
    if event_id:
        url = f"{base}?event={event_id}"
        if audience_scope:
            url += f"&audience_scope={audience_scope}"
        return redirect(url)
    return redirect(base)


def _resolve_step_send_at(step, org):
    """Inline-confirm scheduling rule: a future ``send_at`` schedules for it; a past or
    missing one sends now. Unlike the composer form (which rejects past times), inline
    confirm treats a lapsed suggestion as "send now". Returns ``(scheduled: bool, send_at)``.
    """
    raw = step.get('send_at')
    if raw:
        try:
            dt = datetime.fromisoformat(raw)
            if dt > timezone.now():
                return True, dt
        except (ValueError, TypeError):
            pass
    return False, timezone.now()


def _plan_all_scheduled(steps):
    """True when the plan has at least one step and every step has been scheduled/launched
    (each step carries a launched_campaign_id)."""
    steps = steps or []
    return bool(steps) and all(s.get('launched_campaign_id') for s in steps)


def _save_plan_steps(plan, steps):
    """Persist ``plan.steps`` and keep the derived status in sync: In progress once every
    step is scheduled/launched, else Draft. One save; writes status only when it changes.
    Recompute-based, so it advances on the last launch and never spuriously reverts a
    fully-scheduled plan (steps can't be un-launched)."""
    plan.steps = steps
    fields = ['steps', 'updated_at']
    desired = (SMSCampaignPlan.Status.IN_PROGRESS if _plan_all_scheduled(steps)
               else SMSCampaignPlan.Status.DRAFT)
    if plan.status != desired:
        plan.status = desired
        fields.append('status')
    plan.save(update_fields=fields)


def _mark_plan_step_launched(org, plan_id, step, campaign_id):
    """Idempotently stamp a plan step launched + linked to its campaign.

    No-op if the plan/step is gone or the step is already linked to a campaign, so a
    duplicate confirm (or a composer send that replays an idempotent campaign) never
    double-stamps. Keying the guard on ``launched_campaign_id`` (not ``launched_at``)
    means a legacy step that predates this feature — marked launched by the old
    redirect-only behavior but never actually sent — can still be confirmed. Advances the
    plan to In progress once this makes every step scheduled.
    """
    plan = SMSCampaignPlan.objects.filter(organization=org, id=plan_id).first()
    if not plan:
        return
    steps = plan.steps or []
    if step is None or step < 0 or step >= len(steps):
        return
    if steps[step].get('launched_campaign_id'):
        return
    steps[step] = {
        **steps[step],
        'launched_at': timezone.now().isoformat(),
        'launched_campaign_id': str(campaign_id),
    }
    _save_plan_steps(plan, steps)


def _plan_step_event(org, plan, criteria):
    """The Event a step's campaign should link to (for attribution), from its criteria.

    An ``event_id`` audience (ticket buyers) links to that event. Any other step of an
    event plan — all-subscribers or a market/geo audience — links to the plan's own event,
    since the whole plan was generated to promote it. Segment plans have no event link.
    """
    if criteria.get('event_id'):
        return Event.objects.filter(
            organization=org, deleted_at__isnull=True, id=criteria['event_id'],
        ).first()
    if plan.event_id:
        return plan.event
    return None


@login_required
@require_org
@require_host
@require_sms_feature
@require_POST
def sms_plan_preview_step(request, pk, step):
    """JSON: resolved recipient count + token cost + wallet balance + schedule label for
    one plan step, so the plan page can show an inline confirm panel before sending.

    Never writes. ``count == 0`` / ``exceeds_cap`` still return ``ok: True`` (the panel
    disables confirm and explains) — they're valid preview states, not errors.
    """
    from tickets.services.sms_strategist import format_send_label
    from .services.sms_credits import plan_campaign_footers

    org = get_organization(request)
    if not org.ai_sms_strategist_enabled:
        raise Http404()
    plan = get_object_or_404(
        SMSCampaignPlan.objects.filter(organization=org).select_related('event'), id=pk,
    )

    steps = plan.steps or []
    if step < 0 or step >= len(steps):
        raise Http404()
    target = steps[step]

    override_body = request.POST.get('body')
    body = override_body.strip() if (override_body and override_body.strip()) else target.get('body', '')
    criteria = target.get('audience_criteria') or {}
    cap = getattr(settings, 'SMS_CAMPAIGN_MAX_RECIPIENTS', 5000)
    scheduled, send_at = _resolve_step_send_at(target, org)

    recipients = SMSCampaign(
        organization=org, filter_criteria=criteria,
    ).materialize(org, cap=cap + 1)
    count = len(recipients)
    exceeds_cap = count > cap

    cost_cents, footer_plan = plan_campaign_footers(
        org, body, [r['phone'] for r in recipients], as_of=send_at,
    )
    cost_tokens = sum(footer_plan[r['phone']][1] for r in recipients)
    balance_cents = org.sms_credit_balance_cents
    # Balance shown in tokens, formatted exactly like the composer's confirm panel.
    from tickets.templatetags.tickets_extras import tokens as _tokens
    balance_tokens = _tokens(balance_cents)
    launched_id = target.get('launched_campaign_id')

    return JsonResponse({
        'ok': True,
        'recipient_count': min(count, cap),
        'exceeds_cap': exceeds_cap,
        'cap': cap,
        'cost_cents': cost_cents,
        'cost_tokens': cost_tokens,
        'balance_cents': balance_cents,
        'balance_tokens': balance_tokens,
        'insufficient': cost_cents > balance_cents,
        'scheduled': scheduled,
        'schedule_label': format_send_label(send_at) if scheduled else 'now',
        # Fresh per-preview token echoed back on confirm so a double-click can't
        # create two campaigns.
        'idempotency_key': uuid.uuid4().hex,
        'already_launched': bool(launched_id),
        'launched_campaign_url': (
            reverse('tickets:sms_campaign_detail', args=[launched_id]) if launched_id else None
        ),
    })


@login_required
@require_org
@require_host
@require_sms_feature
@require_POST
def sms_plan_confirm_step(request, pk, step):
    """Create + charge + send/schedule one plan step's campaign inline, then mark the
    step launched + linked. Idempotent: an already-launched step (or a duplicate confirm
    with the same key) returns the existing campaign without charging again.
    """
    org = get_organization(request)
    if not org.ai_sms_strategist_enabled:
        raise Http404()
    plan = get_object_or_404(
        SMSCampaignPlan.objects.filter(organization=org).select_related('event'), id=pk,
    )

    steps = plan.steps or []
    if step < 0 or step >= len(steps):
        raise Http404()
    target = steps[step]

    # Already sent → return the existing campaign; do not re-charge.
    existing_id = target.get('launched_campaign_id')
    if existing_id:
        return JsonResponse({
            'ok': True, 'already_launched': True, 'campaign_id': existing_id,
            'campaign_url': reverse('tickets:sms_campaign_detail', args=[existing_id]),
        })

    # A just-typed (unsaved) body edit is authoritative: apply + persist it so what
    # sends is exactly what the organizer sees.
    override_body = request.POST.get('body')
    if override_body is not None and override_body.strip():
        target = _apply_step_body(target, override_body.strip())
        steps[step] = target
        plan.steps = steps
        plan.save(update_fields=['steps', 'updated_at'])

    criteria = target.get('audience_criteria') or {}
    event = _plan_step_event(org, plan, criteria)
    scheduled, send_at = _resolve_step_send_at(target, org)
    name = f"{plan.name} · {target.get('purpose_label') or target.get('purpose') or 'Message'}"[:200]
    idem_key = request.POST.get('idempotency_key') or uuid.uuid4().hex
    cap = getattr(settings, 'SMS_CAMPAIGN_MAX_RECIPIENTS', 5000)

    from .services.sms_campaigns import (
        finalize_campaign_send, AudienceEmptyError, AudienceTooLargeError,
    )
    from .services.sms_credits import InsufficientCreditsError
    try:
        result = finalize_campaign_send(
            org, name=name, body=target.get('body', ''), criteria=criteria,
            manual_include_ids=[], event=event, scheduled=scheduled, send_at=send_at,
            user=request.user, idempotency_key=idem_key, cap=cap,
        )
    except AudienceEmptyError:
        return JsonResponse(
            {'ok': False, 'error': 'This audience has no contactable recipients.'}, status=400,
        )
    except AudienceTooLargeError as exc:
        return JsonResponse(
            {'ok': False,
             'error': f'This audience resolves to more than {exc.cap} recipients. '
                      'Narrow it before sending.'},
            status=400,
        )
    except InsufficientCreditsError:
        return JsonResponse(
            {'ok': False,
             'error': 'Not enough SMS tokens to send this campaign. Top up to continue.'},
            status=400,
        )

    _mark_plan_step_launched(org, plan.id, step, result.campaign.id)
    return JsonResponse({
        'ok': True,
        'campaign_id': str(result.campaign.id),
        'campaign_url': reverse('tickets:sms_campaign_detail', args=[result.campaign.id]),
        'scheduled': result.scheduled,
    })


@login_required
@require_org
@require_host
@require_sms_feature
@require_POST
def sms_plan_remove_step(request, pk, step):
    """Remove one campaign (step) from a plan's sequence, then re-index the rest.

    ``order`` must stay equal to the list index because the per-step endpoints key
    off it, so the remaining steps are renumbered after the removal.
    """
    org = get_organization(request)
    if not org.ai_sms_strategist_enabled:
        raise Http404()
    plan = get_object_or_404(SMSCampaignPlan.objects.filter(organization=org), id=pk)

    steps = plan.steps or []
    if step < 0 or step >= len(steps):
        raise Http404()

    steps.pop(step)
    for i, s in enumerate(steps):
        s['order'] = i
    # Recompute status: removing the last un-scheduled step advances the plan to In progress.
    _save_plan_steps(plan, steps)
    messages.success(request, 'Removed the message from this plan.')
    return redirect('tickets:sms_plan_detail', pk=plan.id)
