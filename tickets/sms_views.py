"""Views for native marketing SMS: campaigns, recipient lists, and Twilio webhooks.

Kept out of the (very large) views.py for cohesion. Every authenticated view is
org-scoped and gated behind the per-org ``sms_marketing_enabled`` flag.
"""

import logging
import re
import uuid
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
    SMSCampaign, SMSMessageRecipient, PhoneSuppression, Event,
    Customer, TrackingLink, StripeCheckoutSession, Ticket,
    TICKETING_TYPE_DIRECT, EVENT_STATUS_LIVE,
    _generate_tracking_token,
)
from .forms import SMSCampaignForm
from .services.customer_filters import filter_customers
from .services.sms_consent import set_sms_opt_in
from .services.tagging import tag_customers
from .sms import (
    normalize_phone, validate_twilio_request, sms_segment_info, send_sms, extract_first_url,
    with_stop_footer,
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
        criteria = {
            'search': request.POST.get('search') or None,
            'rfm_segment': request.POST.get('segment') or None,
            'tag_id': request.POST.get('tag') or None,
        }
        customers = filter_customers(org, criteria).distinct()
    else:
        posted = [s for s in request.POST.getlist('customer_ids') if s]
        customers = Customer.objects.filter(organization=org, id__in=posted).distinct()

    # Return to the list with the active filters preserved.
    params = urlencode({k: v for k, v in (
        ('search', request.POST.get('search', '')),
        ('segment', request.POST.get('segment', '')),
        ('tag', request.POST.get('tag', '')),
        ('sort', request.POST.get('sort', '')),
    ) if v})
    back = reverse('tickets:customer_list') + (f'?{params}' if params else '')
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
        criteria = {
            'search': request.POST.get('search') or None,
            'rfm_segment': request.POST.get('segment') or None,
            'tag_id': request.POST.get('tag') or None,
        }
        customers = filter_customers(org, criteria).distinct()
    else:
        posted = [s for s in request.POST.getlist('customer_ids') if s]
        customers = Customer.objects.filter(organization=org, id__in=posted).distinct()

    # Return to the list with the active filters preserved.
    params = urlencode({k: v for k, v in (
        ('search', request.POST.get('search', '')),
        ('segment', request.POST.get('segment', '')),
        ('tag', request.POST.get('tag', '')),
        ('sort', request.POST.get('sort', '')),
    ) if v})
    back = reverse('tickets:customer_list') + (f'?{params}' if params else '')

    opt_in = request.POST.get('sms_status') == 'opt_in'
    count = set_sms_opt_in(customers, opt_in=opt_in)
    verb = 'Opted in' if opt_in else 'Opted out'
    if count == 0:
        # Either nothing was selected or every selected customer was already in
        # the target state — nothing actually changed either way.
        messages.info(request, 'No customers needed updating.')
    else:
        messages.success(request, f'{verb} {count} customer(s).')
    return redirect(back)


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
    campaigns = _annotate_counts(
        SMSCampaign.objects.filter(organization=org, deleted_at__isnull=True)
        .select_related('event')
    ).order_by('-created_at')
    paginator = Paginator(campaigns, 25)
    page_obj = paginator.get_page(request.GET.get('page'))

    # Consolidated SMS performance band — reuses the Marketing analytics plumbing
    # (same cache/window as the Overview page) so all SMS lives on one page.
    window_key, window_days, window_label = resolve_window(request.GET.get('window', DEFAULT_WINDOW))
    metrics = get_cached_marketing_metrics(org, window_days, window_key)
    engagement_chart = {
        'labels': [row['month'] for row in metrics['engagement_trends']],
        'sms_clicks': [row['sms_clicks'] for row in metrics['engagement_trends']],
    }

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

    visible = [r for r in series if not selected_market or r['market'] == selected_market]
    audience_points = {'native': [], 'slicktext': []}
    for r in visible:
        audience_points[r['channel']].append({
            'x': r['sent_ms'], 'y': r['audience'], 'name': r['name'], 'market': r['market'],
        })

    # By-market breakdown spans ALL markets (always-on comparison), independent of
    # the chart's market filter.
    breakdown = {}
    for r in series:
        agg = breakdown.setdefault(
            r['market'], {'market': r['market'], 'broadcasts': 0, 'total_audience': 0},
        )
        agg['broadcasts'] += 1
        agg['total_audience'] += r['audience']
    market_breakdown = sorted(breakdown.values(), key=lambda a: a['total_audience'], reverse=True)
    for agg in market_breakdown:
        agg['avg_audience'] = round(agg['total_audience'] / agg['broadcasts']) if agg['broadcasts'] else 0

    return render(request, 'tickets/marketing/sms/campaign_list.html', {
        'page_obj': page_obj,
        'balance_cents': org.sms_credit_balance_cents,
        'sms_native_enabled': org.sms_marketing_enabled,
        'marketing_section': 'sms',
        'window_choices': WINDOW_CHOICES,
        'window_key': window_key,
        'window_label': window_label,
        'native_sms': metrics['native_sms'],
        'sms_channel': metrics['channels']['sms'],
        'top_sms_campaigns': metrics['top_sms_campaigns'],
        'engagement_chart_json': json.dumps(engagement_chart),
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
    # selection silently resets to 'event' between review and confirm.
    audience_scope = request.POST.get('audience_scope') or 'event'
    if audience_scope not in ('event', 'all', 'tag'):
        audience_scope = 'event'

    if request.method == 'POST':
        form = SMSCampaignForm(request.POST, organization=org, event=event)
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
                from .services.sms_credits import (
                    plan_campaign_footers, charge, InsufficientCreditsError,
                )
                # Anchor the per-recipient footer/disclosure decision (and therefore the
                # cost) on the actual send time: a far-future scheduled send must re-disclose
                # phones that will have aged out of the window by then. Computed once here
                # and reused for both the displayed cost and the charge → displayed == charged.
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
                    # Idempotency: if this exact confirm already produced a campaign,
                    # return it instead of charging/sending again.
                    existing = SMSCampaign.objects.filter(
                        organization=org, idempotency_key=idem_key,
                    ).first()
                    if existing:
                        return redirect('tickets:sms_campaign_detail', pk=existing.id)

                    # Always persist as SCHEDULED (send-now → scheduled for now). The
                    # */5 cron is then a safety net: if the immediate dispatch is
                    # dropped, the campaign is still picked up and sent (no lost money).
                    # `scheduled` / `send_at` were resolved above (they anchor the cost).
                    try:
                        with transaction.atomic():
                            campaign = form.save(commit=False)
                            campaign.organization = org
                            campaign.created_by = request.user
                            campaign.event = event
                            campaign.filter_criteria = criteria
                            campaign.link_url = extract_first_url(campaign.body)
                            # Per-campaign attribution: swap any shared event ticket
                            # link for one unique to this campaign (mutates body/link_url).
                            _mint_campaign_tracking_link(org, campaign)
                            campaign.idempotency_key = idem_key
                            campaign.status = SMSCampaign.Status.SCHEDULED
                            campaign.scheduled_at = send_at
                            campaign.audience_size = len(recipients)
                            campaign.save()
                            # Freeze the audience now so charged == what sends. The
                            # orchestrator reuses these rows (it only re-resolves when
                            # none exist) and re-checks opt-out per recipient at send.
                            SMSMessageRecipient.objects.bulk_create([
                                SMSMessageRecipient(
                                    campaign=campaign, customer_id=r['customer_id'], phone=r['phone'],
                                    stop_disclosed=footer_plan[r['phone']][0],
                                    segments=footer_plan[r['phone']][1],
                                ) for r in recipients
                            ], batch_size=500)
                            # Charge inside the same transaction — campaign + snapshot +
                            # debit are all-or-nothing (no uncharged campaign can exist).
                            charge(org.id, confirm_cost_cents, campaign=campaign,
                                   description=f'Campaign: {campaign.name}',
                                   created_by=request.user)
                    except InsufficientCreditsError:
                        insufficient_credits = True
                        form.add_error(
                            None,
                            'Not enough SMS tokens to send this campaign. Top up to continue.',
                        )
                    except IntegrityError:
                        # Concurrent duplicate confirm (same idempotency_key) — the other
                        # request won; return that campaign.
                        existing = SMSCampaign.objects.filter(
                            organization=org, idempotency_key=idem_key,
                        ).first()
                        if existing:
                            return redirect('tickets:sms_campaign_detail', pk=existing.id)
                        raise
                    else:
                        if not scheduled:
                            transaction.on_commit(
                                lambda cid=str(campaign.id): send_sms_campaign_task.delay(cid)
                            )
                            messages.success(
                                request, f'Sending "{campaign.name}" to {len(recipients)} recipients.',
                            )
                        else:
                            messages.success(
                                request,
                                f'Campaign scheduled for {campaign.scheduled_at:%b %d, %Y %I:%M %p}.',
                            )
                        return redirect('tickets:sms_campaign_detail', pk=campaign.id)
    else:
        initial = {'name': event.name} if event else {}
        form = SMSCampaignForm(organization=org, event=event, initial=initial)

    # Live direct-ticketing events whose buy page is on sale — offered in the
    # composer's "Add a ticket link" dropdown. effective_status is a property, so
    # filter the (small) set of live direct events in Python.
    ticket_link_events = [
        {'id': str(ev.id), 'name': ev.name}
        for ev in Event.objects.filter(
            organization=org, deleted_at__isnull=True,
            ticketing_type=TICKETING_TYPE_DIRECT, status=EVENT_STATUS_LIVE,
        ).order_by('-start_date')
        if ev.effective_status == EVENT_STATUS_LIVE
    ]

    # Initial meter values match billing: segments are counted on the body plus
    # the auto-appended STOP footer (the JS meter mirrors this).
    encoding, segments = sms_segment_info(
        with_stop_footer(request.POST.get('body', '') if request.method == 'POST' else '')
    )
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
        'footer_disclosure_days': getattr(settings, 'SMS_FOOTER_DISCLOSURE_DAYS', 30),
    })


@login_required
@require_org
@require_host
@require_sms_feature
@require_POST
def sms_ticket_link(request):
    """JSON: get-or-create a shared 'SMS' tracking link for a direct, live event and
    return its absolute /t/<token>/ URL for insertion into a campaign body."""
    org = get_organization(request)
    event = get_object_or_404(
        Event.objects.filter(
            organization=org, deleted_at__isnull=True,
            ticketing_type=TICKETING_TYPE_DIRECT,
        ),
        id=request.POST.get('event'),
    )
    if event.effective_status != EVENT_STATUS_LIVE:
        return JsonResponse({'error': 'This event is not on sale.'}, status=400)
    link, _ = TrackingLink.objects.get_or_create(
        organization=org, event=event, name='SMS',
        defaults={'token': _generate_tracking_token()},
    )
    # Use SITE_URL (the public/tunnel host the send pipeline uses) so the link
    # stored in the body resolves for recipients — request.build_absolute_uri
    # would bake in the composer's host (e.g. localhost). Falls back to the
    # request host only when SITE_URL is unset.
    path = reverse('tickets:track_link_redirect', kwargs={'token': link.token})
    site_url = getattr(settings, 'SITE_URL', '').rstrip('/')
    url = f"{site_url}{path}" if site_url else request.build_absolute_uri(path)
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

_TWILIO_STATUS_MAP = {
    'queued': SMSMessageRecipient.Status.QUEUED,
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

    recipient.status = new_status
    if new_status == SMSMessageRecipient.Status.DELIVERED and not recipient.delivered_at:
        recipient.delivered_at = timezone.now()
    if new_status in (SMSMessageRecipient.Status.FAILED, SMSMessageRecipient.Status.UNDELIVERED):
        recipient.error_code = request.POST.get('ErrorCode', '') or recipient.error_code
        recipient.error_message = request.POST.get('ErrorMessage', '') or recipient.error_message
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
