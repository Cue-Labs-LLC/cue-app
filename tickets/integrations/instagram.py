"""Instagram DM support agent — settings views.

Phase 1 ships the per-org FAQ editor only (the knowledge the agent answers
from). Connection / OAuth and the transport pipeline land in later phases;
until then the integration card shows "not connected" and links here.

The FAQ list page is an inline, drag-and-drop editor (progressive enhancement
over the plain create/edit/delete pages): the create/edit/delete views answer
JSON when called via XMLHttpRequest and otherwise redirect, so the standalone
form pages keep working without JavaScript.
"""

import json
import logging
import secrets
import uuid
from dataclasses import asdict
from datetime import timedelta
from functools import wraps
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import (
    Case, Count, IntegerField, OuterRef, Q, Subquery, Value, When,
)
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from ..feature_flags import instagram_dm_agent_enabled
from ..forms import OrgFAQForm
from ..models import InstagramConversation, InstagramMessage, Organization, OrgFAQ
from ..services.instagram import (
    exchange_for_long_lived_instagram_token,
    exchange_instagram_code_for_token,
    get_sender,
    ig_oauth_scopes,
)
from ..services.instagram.graph_client import AUTHORIZE_URL
from ..utils import get_organization, require_admin, require_org

logger = logging.getLogger(__name__)


def _org_faqs(org):
    """Live (non-soft-deleted) FAQs for an org, in display order."""
    return OrgFAQ.objects.filter(organization=org, deleted_at__isnull=True)


def _is_ajax(request):
    return request.headers.get('x-requested-with') == 'XMLHttpRequest'


def require_instagram_feature(view_func):
    """404 the Instagram integration UX unless the org has the feature flag enabled.

    Master rollout gate: the code can ship to everyone while the Instagram UX
    (connection settings, content analytics) stays invisible until
    ``instagram_feature_enabled`` is turned on for a specific org. Never applied to the
    public webhook.
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        org = get_organization(request)
        if org is None or not org.instagram_feature_enabled:
            raise Http404()
        return view_func(request, *args, **kwargs)
    return _wrapped


def require_instagram_dm_agent(view_func):
    """404 the DM-agent UX (inbox, FAQ editor, auto-answer toggle) unless the agent is on.

    Stricter than ``require_instagram_feature``: also requires the global
    ``INSTAGRAM_DM_AGENT_ENABLED`` switch, so the agent can be turned off everywhere while
    the Instagram connection + content analytics keep working.
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not instagram_dm_agent_enabled(get_organization(request)):
            raise Http404()
        return view_func(request, *args, **kwargs)
    return _wrapped


def _faq_json(faq):
    return {
        'id': str(faq.id),
        'question': faq.question,
        'answer': faq.answer,
        'topic': faq.topic,
        'is_published': faq.is_published,
        'sort_order': faq.sort_order,
    }


def _message_json(message, conversation):
    """Serialize a thread message for the live-poll endpoint.

    Row class + meta label are computed here (not in JS) so the bubble matches the
    server-rendered template exactly. ``content`` is raw text; the client inserts it via
    ``textContent`` (CSS already applies ``white-space: pre-wrap``), so no escaping here.
    """
    from django.template.defaultfilters import date as date_filter
    from django.utils.timezone import localtime

    outbound = message.direction == InstagramMessage.DIRECTION_OUTBOUND
    if outbound:
        row_class = 'out'
        if message.author == InstagramMessage.AUTHOR_HUMAN:
            row_class += ' human'
        if message.status == InstagramMessage.STATUS_FAILED:
            row_class += ' failed'
        if message.author == InstagramMessage.AUTHOR_HUMAN:
            meta_prefix = 'You'
        elif message.author == InstagramMessage.AUTHOR_SYSTEM:
            meta_prefix = 'Auto-reply'
        else:
            meta_prefix = 'Agent'
        status_display = message.get_status_display()
    else:
        row_class = 'in'
        meta_prefix = conversation.ig_username or conversation.ig_user_id
        status_display = ''

    return {
        'id': str(message.id),
        'created_at': message.created_at.isoformat(),
        'created_display': date_filter(localtime(message.created_at), 'M j, g:i a'),
        'row_class': row_class,
        'meta_prefix': meta_prefix,
        'status_display': status_display,
        'content': message.content,
    }


@login_required
@require_org
@require_admin
@require_instagram_dm_agent
def instagram_faq_list(request):
    """Inline, drag-and-drop editor for this org's support-agent FAQs."""
    org = get_organization(request)
    faqs = list(_org_faqs(org))
    return render(request, 'tickets/instagram_faq_list.html', {
        'faqs': faqs,
        'is_connected': bool(
            org.instagram_page_access_token and org.instagram_business_account_id
        ),
    })


@login_required
@require_org
@require_admin
@require_instagram_dm_agent
@require_http_methods(["GET", "POST"])
def instagram_faq_create(request):
    """Create a FAQ. Answers JSON for inline (AJAX) adds; redirects otherwise."""
    org = get_organization(request)
    if request.method == 'POST':
        form = OrgFAQForm(request.POST)
        if form.is_valid():
            faq = form.save(commit=False)
            faq.organization = org
            if faq.sort_order is None:
                faq.sort_order = _org_faqs(org).count()
            faq.save()
            if _is_ajax(request):
                return JsonResponse({'ok': True, 'faq': _faq_json(faq)})
            messages.success(request, 'FAQ added.')
            return redirect('tickets:instagram_faq_list')
        if _is_ajax(request):
            return JsonResponse({'ok': False, 'errors': form.errors}, status=400)
    else:
        form = OrgFAQForm()
    return render(request, 'tickets/instagram_faq_form.html', {
        'form': form,
        'action': 'Create',
    })


@login_required
@require_org
@require_admin
@require_instagram_dm_agent
@require_http_methods(["GET", "POST"])
def instagram_faq_edit(request, faq_id):
    """Edit a FAQ (org-scoped). Answers JSON for inline edits; redirects otherwise."""
    org = get_organization(request)
    faq = get_object_or_404(_org_faqs(org), id=faq_id)
    if request.method == 'POST':
        form = OrgFAQForm(request.POST, instance=faq)
        if form.is_valid():
            faq = form.save()
            if _is_ajax(request):
                return JsonResponse({'ok': True, 'faq': _faq_json(faq)})
            messages.success(request, 'FAQ updated.')
            return redirect('tickets:instagram_faq_list')
        if _is_ajax(request):
            return JsonResponse({'ok': False, 'errors': form.errors}, status=400)
    else:
        form = OrgFAQForm(instance=faq)
    return render(request, 'tickets/instagram_faq_form.html', {
        'form': form,
        'faq': faq,
        'action': 'Edit',
    })


@login_required
@require_org
@require_admin
@require_instagram_dm_agent
@require_http_methods(["GET", "POST"])
def instagram_faq_delete(request, faq_id):
    """Soft-delete a FAQ (org-scoped). Answers JSON for inline deletes."""
    org = get_organization(request)
    faq = get_object_or_404(_org_faqs(org), id=faq_id)
    if request.method == 'POST':
        faq.delete()  # AuditBaseModel soft delete
        if _is_ajax(request):
            return JsonResponse({'ok': True})
        messages.success(request, 'FAQ deleted.')
        return redirect('tickets:instagram_faq_list')
    return render(request, 'tickets/instagram_faq_delete.html', {'faq': faq})


@login_required
@require_org
@require_admin
@require_instagram_dm_agent
@require_http_methods(["POST"])
def instagram_faq_reorder(request):
    """AJAX: persist the new FAQ order after a drag-and-drop."""
    org = get_organization(request)
    try:
        data = json.loads(request.body)
        ids = [uuid.UUID(str(i)) for i in data.get('order', [])]
    except (json.JSONDecodeError, ValueError, TypeError):
        return JsonResponse({'error': 'Invalid payload'}, status=400)

    faqs = {f.id: f for f in _org_faqs(org).filter(id__in=ids)}
    to_update = []
    for position, faq_id in enumerate(ids):
        faq = faqs.get(faq_id)
        if faq is not None:
            faq.sort_order = position
            to_update.append(faq)
    with transaction.atomic():
        OrgFAQ.objects.bulk_update(to_update, ['sort_order'])
    return JsonResponse({'ok': True})


# ---------------------------------------------------------------------------
# Instagram OAuth connection + runtime toggle (Phase 7)
#
# Uses "Instagram API with Instagram Login" (graph.instagram.com) — connect →
# Instagram OAuth dialog with a session `state` CSRF token → callback → long-lived
# token. No Facebook Page is involved; the connected Instagram professional account
# authenticates directly. The callback persists the credentials the Graph sender +
# webhook resolver need. The whole feature stays behind the admin-only
# `instagram_feature_enabled` gate via @require_instagram_feature.
# ---------------------------------------------------------------------------


def _instagram_configured() -> bool:
    return bool(settings.INSTAGRAM_APP_ID and settings.INSTAGRAM_APP_SECRET)


@login_required
@require_org
@require_admin
@require_instagram_feature
@require_http_methods(["GET"])
def instagram_settings(request):
    """Show Instagram connection state + the runtime auto-answer toggle."""
    return render(request, 'tickets/settings_instagram.html', {
        'callback_url': request.build_absolute_uri(reverse('tickets:instagram_callback')),
        'instagram_configured': _instagram_configured(),
    })


@login_required
@require_org
@require_admin
@require_instagram_feature
@require_http_methods(["POST"])
def instagram_connect(request):
    """Start Instagram Business Login for Instagram Messaging access."""
    if not _instagram_configured():
        messages.error(request, 'Instagram is not configured. Add INSTAGRAM_APP_ID and INSTAGRAM_APP_SECRET.')
        return redirect('tickets:instagram_settings')

    state = secrets.token_urlsafe(32)
    request.session['instagram_oauth_state'] = state
    callback_url = request.build_absolute_uri(reverse('tickets:instagram_callback'))
    params = urlencode({
        'client_id': settings.INSTAGRAM_APP_ID,
        'redirect_uri': callback_url,
        'state': state,
        # Messaging access is only requested while the DM agent is on; insights always.
        'scope': ig_oauth_scopes(
            include_messaging=instagram_dm_agent_enabled(get_organization(request)),
        ),
        'response_type': 'code',
    })
    return redirect(f'{AUTHORIZE_URL}?{params}')


@login_required
@require_org
@require_admin
@require_instagram_feature
@require_http_methods(["GET"])
def instagram_callback(request):
    """Handle the Instagram OAuth callback and persist the Instagram credentials."""
    from ..services.instagram import InstagramGraphAPIError, InstagramGraphClient

    org = get_organization(request)
    expected_state = request.session.pop('instagram_oauth_state', None)
    if not expected_state or request.GET.get('state') != expected_state:
        messages.error(request, 'Instagram connection could not be verified. Please try again.')
        return redirect('tickets:instagram_settings')

    if request.GET.get('error'):
        messages.error(request, request.GET.get('error_description') or 'Instagram authorization was cancelled.')
        return redirect('tickets:instagram_settings')

    code = request.GET.get('code')
    if not code:
        messages.error(request, 'Instagram did not return an authorization code.')
        return redirect('tickets:instagram_settings')

    callback_url = request.build_absolute_uri(reverse('tickets:instagram_callback'))
    try:
        short_token = exchange_instagram_code_for_token(code, callback_url)
        user_token = short_token['access_token']
    except (KeyError, InstagramGraphAPIError) as exc:
        messages.error(request, f'Could not connect Instagram: {exc}')
        return redirect('tickets:instagram_settings')

    # Upgrade to a 60-day token when possible, but don't fail the connection if the
    # long-lived exchange hiccups — the short-lived token (≈1h) still lets us verify the
    # account and send. Token refresh is handled separately (D13).
    expires_in = 3600
    try:
        long_token = exchange_for_long_lived_instagram_token(user_token)
        user_token = long_token.get('access_token') or user_token
        expires_in = long_token.get('expires_in') or expires_in
    except InstagramGraphAPIError as exc:
        logger.warning("Long-lived IG token exchange failed; using short-lived token: %s", exc)

    try:
        me = InstagramGraphClient(user_token).get_me()
    except (KeyError, InstagramGraphAPIError) as exc:
        logger.warning("Instagram get_me failed: %s", exc)
        messages.error(
            request,
            "Connected to Instagram, but couldn't read the account. This usually means the "
            "Instagram account isn't a professional (Business or Creator) account, or it "
            "hasn't accepted the Instagram Tester invite for this app while it's in "
            "development. Fix that and reconnect.",
        )
        return redirect('tickets:instagram_settings')

    # The IGSID that arrives in webhook entry[].id — the org-resolution routing key.
    ig_account_id = str(me.get('user_id') or short_token.get('user_id') or '')
    if not ig_account_id:
        messages.error(request, 'Instagram did not return an account id. Please try again.')
        return redirect('tickets:instagram_settings')

    # D11: inbound webhooks route to an org by instagram_business_account_id, so it must
    # map to at most one org. Fail with a clear message instead of an IntegrityError.
    if Organization.objects.filter(
        instagram_business_account_id=ig_account_id,
    ).exclude(pk=org.pk).exists():
        messages.error(request, 'This Instagram account is already connected to another workspace.')
        return redirect('tickets:instagram_settings')

    dm_agent_on = instagram_dm_agent_enabled(org)
    if dm_agent_on:
        try:
            InstagramGraphClient(user_token).subscribe_to_messages()
        except InstagramGraphAPIError as exc:
            messages.error(request, f'Connected, but could not subscribe to messages: {exc}')
            return redirect('tickets:instagram_settings')

    org.instagram_page_access_token = user_token  # IG user token (no Page in this flow)
    org.instagram_business_account_id = ig_account_id
    org.instagram_page_id = ''
    org.instagram_username = me.get('username', '')
    org.instagram_token_expires_at = (
        timezone.now() + timedelta(seconds=int(expires_in)) if expires_in else None
    )
    org.save(update_fields=[
        'instagram_page_access_token',
        'instagram_business_account_id',
        'instagram_page_id',
        'instagram_username',
        'instagram_token_expires_at',
    ])
    handle = org.instagram_username or ig_account_id
    if dm_agent_on:
        messages.success(
            request,
            f'Instagram connected (@{handle}). Turn on the support agent below once your '
            'FAQs are ready.',
        )
    else:
        messages.success(request, f'Instagram connected (@{handle}).')
    return redirect('tickets:instagram_settings')


@login_required
@require_org
@require_admin
@require_instagram_feature
@require_http_methods(["POST"])
def instagram_disconnect(request):
    """Clear the Instagram credentials for the current org (keeps FAQ/inbox data)."""
    org = get_organization(request)
    org.instagram_page_access_token = ''
    org.instagram_business_account_id = ''
    org.instagram_page_id = ''
    org.instagram_username = ''
    org.instagram_token_expires_at = None
    org.save(update_fields=[
        'instagram_page_access_token',
        'instagram_business_account_id',
        'instagram_page_id',
        'instagram_username',
        'instagram_token_expires_at',
    ])
    messages.success(request, 'Instagram disconnected.')
    return redirect('tickets:instagram_settings')


@login_required
@require_org
@require_admin
@require_instagram_dm_agent
@require_http_methods(["POST"])
def instagram_toggle_agent(request):
    """Flip the runtime auto-answer gate (instagram_support_agent_enabled)."""
    org = get_organization(request)
    org.instagram_support_agent_enabled = not org.instagram_support_agent_enabled
    org.save(update_fields=['instagram_support_agent_enabled'])
    state = 'on' if org.instagram_support_agent_enabled else 'off'
    messages.success(request, f'Instagram support agent turned {state}.')
    return redirect('tickets:instagram_settings')


# ---------------------------------------------------------------------------
# Organic content analytics
#
# Per-post and per-format organic performance for the connected account, read
# live from graph.instagram.com (cached ~30 min). Reuses the DM agent's
# connection; insights need the instagram_business_manage_insights scope, so an
# account connected before that scope existed is asked to reconnect.
# ---------------------------------------------------------------------------

@login_required
@require_org
@require_admin
@require_instagram_feature
@require_http_methods(["GET"])
def instagram_content_insights(request):
    """Organic Instagram content performance: posts, reels, and format comparison."""
    from ..services.instagram import InstagramGraphAPIError
    from ..services.instagram.content_insights import (
        DEFAULT_GROUP,
        DEFAULT_SORT,
        DEFAULT_WINDOW,
        FORMAT_LABELS,
        GROUPS,
        SORTS,
        WINDOW_CHOICES,
        InstagramContentInsightsCalculator,
    )

    org = get_organization(request)
    calculator = InstagramContentInsightsCalculator(
        org,
        window=request.GET.get('window'),
        sort=request.GET.get('sort'),
        fmt=request.GET.get('format'),
        group=request.GET.get('group'),
    )
    connected = bool(org.instagram_page_access_token and org.instagram_business_account_id)

    data, error, rate_limited, token_expired = None, None, False, False
    if connected:
        try:
            data = calculator.calculate(refresh=bool(request.GET.get('refresh')))
        except InstagramGraphAPIError as exc:
            error = str(exc)
            rate_limited = exc.is_rate_limited
            token_expired = exc.is_token_error

    # Every control links to the current state with one value changed, so window /
    # format / grouping / sort always preserve each other. Defaults are left out to keep
    # URLs short.
    state = {
        'window': calculator.window,
        'format': calculator.format_filter,
        'group': calculator.group,
        'sort': calculator.sort,
    }
    defaults = {
        'window': DEFAULT_WINDOW, 'format': '', 'group': DEFAULT_GROUP, 'sort': DEFAULT_SORT,
    }

    def link(anchor='', **changes):
        params = {**state, **changes}
        query = urlencode({k: v for k, v in params.items() if v and v != defaults[k]})
        return f"?{query}{anchor}" if query else f"?{anchor}" if anchor else '?'

    format_links = [('', 'All', link(format=''))] + [
        (key, label, link(format=key)) for key, label in FORMAT_LABELS.items()
    ]

    return render(request, 'tickets/instagram_content.html', {
        'connected': connected,
        'data': data,
        'error': error,
        'rate_limited': rate_limited,
        'token_expired': token_expired,
        'window_links': [(key, label, link(window=key)) for key, label, _days in WINDOW_CHOICES],
        'active_window': calculator.window,
        'active_window_label': calculator.window_label,
        'sort_links': [(key, label, link(sort=key)) for key, (label, _field) in SORTS.items()],
        'active_sort': calculator.sort,
        'active_sort_label': calculator.sort_label,
        'format_links': format_links,
        'active_format': calculator.format_filter,
        # Grouping only changes the chart, so its links jump back to the chart.
        'group_links': [(key, label, link('#reach', group=key)) for key, label in GROUPS.items()],
        'active_group': calculator.group,
        'current_url': link(),
        'refresh_url': link() + ('&' if link() != '?' else '') + 'refresh=1',
        'all_formats_url': link(format=''),
        'has_chart': bool(data and data['chart']['labels']),
    })


@login_required
@require_org
@require_admin
@require_instagram_feature
@require_http_methods(["GET"])
def instagram_post_detail(request, media_id):
    """HTML fragment for the Instagram Content page's post-detail side panel.

    Fetched when a row is clicked rather than embedded per post, so a 12-month page
    doesn't carry hundreds of hidden detail blocks. Reads the same cached pull as the
    page (no Meta call unless the cache is cold; carousels add one cached call).
    """
    from ..services.instagram import InstagramGraphAPIError
    from ..services.instagram.content_insights import InstagramContentInsightsCalculator

    org = get_organization(request)
    if not (org.instagram_page_access_token and org.instagram_business_account_id):
        raise Http404()
    calculator = InstagramContentInsightsCalculator(org, window=request.GET.get('window'))
    try:
        detail = calculator.post_detail(media_id)
    except InstagramGraphAPIError:
        return HttpResponse(
            '<p class="text-danger">Couldn\'t load this post from Instagram. Try again.</p>',
            status=502,
        )
    if detail is None:
        raise Http404()
    from django.utils.timezone import localtime
    post = detail['post']
    near_events, other_events = _event_choices(
        org, localtime(post['published_at']).date(), [e.pk for e in post['events']],
    )
    return render(request, 'tickets/_instagram_post_detail.html', {
        **detail,
        'near_events': near_events,
        'other_events': other_events,
        'next_url': request.GET.get('next', ''),
    })


def _org_events(org):
    from ..models import Event
    return Event.objects.filter(organization=org, deleted_at__isnull=True)


def _event_choices(org, post_date, linked_ids):
    """Events for the panel's "link to event" picker: the ones around the post date
    first (promo usually runs up to ~3 months ahead), then everything else."""
    from datetime import timedelta

    events = list(
        _org_events(org).exclude(pk__in=linked_ids)
        .select_related('venue').order_by('-start_date')[:300]
    )
    near = [
        e for e in events
        if post_date - timedelta(days=7) <= e.start_date <= post_date + timedelta(days=90)
    ]
    near.sort(key=lambda e: e.start_date)
    near_ids = {e.pk for e in near}
    return near, [e for e in events if e.pk not in near_ids]


@login_required
@require_org
@require_admin
@require_instagram_feature
@require_http_methods(["POST"])
def instagram_post_event_link(request, media_id):
    """Link (``action=link``) or unlink (``action=unlink``) a post and an event.

    Used by the post-detail panel and the event Marketing tab card. Linking requires the
    post to be in the org's recent Instagram pull; unlinking doesn't (the post may have
    aged out or been deleted). Answers JSON for AJAX, otherwise redirects back.
    """
    from ..models import InstagramPostEvent
    from ..services.instagram import InstagramGraphAPIError
    from ..services.instagram.content_insights import InstagramContentInsightsCalculator

    org = get_organization(request)
    action = request.POST.get('action', 'link')
    event_id = request.POST.get('event_id')
    event = _org_events(org).filter(pk=event_id).first() if _is_uuid(event_id) else None
    next_url = request.POST.get('next') or ''
    back = next_url if next_url.startswith('/') and not next_url.startswith('//') else \
        reverse('tickets:instagram_content_insights')

    def fail(message, status=400):
        if _is_ajax(request):
            return JsonResponse({'ok': False, 'error': message}, status=status)
        messages.error(request, message)
        return redirect(back)

    if event is None:
        return fail('Pick an event.', status=404)
    if action == 'unlink':
        InstagramPostEvent.objects.filter(
            organization=org, event=event, ig_media_id=media_id,
        ).delete()
    elif action == 'link':
        if not (org.instagram_page_access_token and org.instagram_business_account_id):
            return fail('Connect Instagram first.')
        try:
            posts = InstagramContentInsightsCalculator(org).raw_posts()
        except InstagramGraphAPIError:
            return fail("Couldn't reach Instagram to verify this post. Try again.", status=502)
        if not any(p['id'] == media_id for p in posts):
            return fail("That post isn't in your recent Instagram posts.", status=404)
        InstagramPostEvent.objects.get_or_create(
            organization=org, event=event, ig_media_id=media_id,
            defaults={'linked_by': request.user},
        )
    else:
        return fail('Unknown action.')

    if _is_ajax(request):
        return JsonResponse({
            'ok': True,
            'linked': action == 'link',
            'event': {
                'id': str(event.pk),
                'name': event.name,
                'date': event.start_date.strftime('%b %-d, %Y'),
                'url': reverse('tickets:event_detail', args=[event.pk]),
            },
        })
    messages.success(request, 'Post linked to event.' if action == 'link' else 'Post unlinked.')
    return redirect(back)


def _is_uuid(value):
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


@login_required
@require_org
@require_admin
@require_instagram_feature
@require_http_methods(["GET"])
def event_instagram_card(request, event_id):
    """HTML fragment for the Instagram card on an event's Marketing tab.

    Fetched when the tab opens so the event page never waits on Instagram.
    """
    from ..services.instagram import InstagramGraphAPIError
    from ..services.instagram.event_content import EventInstagramContentCalculator

    org = get_organization(request)
    event = get_object_or_404(_org_events(org).select_related('venue', 'market'), pk=event_id)
    context = {'event': event, 'org': org, 'data': None, 'error': None}
    if not (org.instagram_page_access_token and org.instagram_business_account_id):
        context['not_connected'] = True
    else:
        try:
            context['data'] = EventInstagramContentCalculator(org, event).calculate()
        except InstagramGraphAPIError as exc:
            context['error'] = 'expired' if exc.is_token_error else (
                'rate_limited' if exc.is_rate_limited else str(exc)
            )
    return render(request, 'tickets/marketing/_instagram_event_card_body.html', context)


# ---------------------------------------------------------------------------
# Inbound webhook (Phase 3)
#
# Meta delivers every subscribed event for the app to this one public URL. The
# view is deliberately thin and ALWAYS returns 200 quickly on a verified POST —
# a non-200 makes Meta retry and eventually disable the subscription — doing the
# real work in a Celery task. GET is Meta's one-time verification handshake.
# ---------------------------------------------------------------------------

@csrf_exempt
@require_http_methods(["GET", "POST"])
def instagram_webhook(request):
    """Meta Instagram messaging webhook: GET verify handshake + signed POST events."""
    from ..services.instagram import normalize_meta_payload, verify_meta_signature
    from ..tasks import process_instagram_inbound_task

    if request.method == 'GET':
        mode = request.GET.get('hub.mode')
        token = request.GET.get('hub.verify_token')
        challenge = request.GET.get('hub.challenge', '')
        expected = getattr(settings, 'INSTAGRAM_WEBHOOK_VERIFY_TOKEN', '')
        if mode == 'subscribe' and expected and token == expected:
            return HttpResponse(challenge)
        return HttpResponse(status=403)

    # POST
    if not verify_meta_signature(request):
        return HttpResponse(status=403)

    if not settings.INSTAGRAM_DM_AGENT_ENABLED:
        # DM agent switched off: ack so Meta doesn't retry (or eventually disable the
        # subscription), but don't persist or answer anything.
        return HttpResponse(status=200)

    try:
        body = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        # Signed by Meta but unparseable — ack so Meta doesn't retry; nothing to do.
        return HttpResponse(status=200)

    for item in normalize_meta_payload(body):
        org = Organization.objects.filter(
            instagram_business_account_id=item.ig_account_id,
        ).first()
        if org is None:
            # Unknown account — likely a misconfiguration (webhook entry[].id not
            # persisted into instagram_business_account_id). Log so it's debuggable
            # rather than a silent 100% drop.
            logger.warning(
                "IG webhook: no org for instagram_business_account_id=%s",
                item.ig_account_id,
            )
            continue
        process_instagram_inbound_task.delay(str(org.id), asdict(item))

    return HttpResponse(status=200)


# ---------------------------------------------------------------------------
# Human-review inbox (Phase 4)
#
# When the agent can't auto-send (sensitive / low-confidence / ungrounded), it
# queues a draft (status=pending_review) and flags the conversation
# awaiting_human. These views let an admin read the thread and approve/edit/
# discard the draft or send their own reply. All sends go through get_sender —
# the StubSender until the real Graph sender lands in Phase 7.
# ---------------------------------------------------------------------------

def _maybe_resolve(conversation):
    """Resolve a flagged conversation once no drafts remain pending review."""
    still_pending = conversation.messages.filter(
        direction=InstagramMessage.DIRECTION_OUTBOUND,
        status=InstagramMessage.STATUS_PENDING_REVIEW,
    ).exists()
    if not still_pending and conversation.status == InstagramConversation.STATUS_AWAITING_HUMAN:
        conversation.status = InstagramConversation.STATUS_RESOLVED
        conversation.save(update_fields=['status'])


@login_required
@require_org
@require_admin
@require_instagram_dm_agent
def instagram_inbox(request):
    """List this org's DM threads: awaiting-human first, then with-human, open, resolved."""
    org = get_organization(request)
    status_rank = Case(
        When(status=InstagramConversation.STATUS_AWAITING_HUMAN, then=Value(0)),
        When(status=InstagramConversation.STATUS_HUMAN_HANDLING, then=Value(1)),
        When(status=InstagramConversation.STATUS_OPEN, then=Value(2)),
        default=Value(3),
        output_field=IntegerField(),
    )
    last_message = InstagramMessage.objects.filter(
        conversation=OuterRef('pk'),
    ).order_by('-created_at')
    conversations = (
        InstagramConversation.objects.filter(organization=org)
        .annotate(
            pending_count=Count(
                'messages',
                filter=Q(
                    messages__direction=InstagramMessage.DIRECTION_OUTBOUND,
                    messages__status=InstagramMessage.STATUS_PENDING_REVIEW,
                ),
            ),
            last_preview=Subquery(last_message.values('content')[:1]),
            _status_rank=status_rank,
        )
        .order_by('_status_rank', '-last_message_at')
    )
    return render(request, 'tickets/instagram_inbox.html', {
        'conversations': conversations,
    })


@login_required
@require_org
@require_admin
@require_instagram_dm_agent
def instagram_conversation_detail(request, conversation_id):
    """Show one DM thread with the pending draft (if any) ready to approve/edit."""
    org = get_organization(request)
    conversation = get_object_or_404(
        InstagramConversation.objects.filter(organization=org), id=conversation_id,
    )
    all_messages = list(conversation.messages.all())  # ordered by created_at
    pending_draft = next(
        (m for m in reversed(all_messages)
         if m.direction == InstagramMessage.DIRECTION_OUTBOUND
         and m.status == InstagramMessage.STATUS_PENDING_REVIEW),
        None,
    )
    # The timeline shows only what actually reached (or came from) the customer.
    # Un-sent drafts — pending_review (shown in the editor below) and discarded —
    # never went out, so they'd be misleading rendered as sent bubbles.
    hidden_inline = {
        InstagramMessage.STATUS_PENDING_REVIEW,
        InstagramMessage.STATUS_DISCARDED,
    }
    thread_messages = [
        m for m in all_messages
        if not (m.direction == InstagramMessage.DIRECTION_OUTBOUND
                and m.status in hidden_inline)
    ]
    return render(request, 'tickets/instagram_conversation_detail.html', {
        'conversation': conversation,
        'thread_messages': thread_messages,
        'pending_draft': pending_draft,
    })


@login_required
@require_org
@require_admin
@require_instagram_dm_agent
@require_http_methods(["GET"])
def instagram_conversation_messages(request, conversation_id):
    """JSON feed of thread messages for the detail page's live-poll loop.

    Returns messages at/after the ``?after=<iso8601>`` cursor (all when omitted), plus the
    conversation's current control state so the client can detect a structural change and
    reload. Applies the same visibility filter as ``instagram_conversation_detail`` (no
    un-sent drafts). Uses ``created_at__gte`` so a boundary message is never skipped; the
    client de-dupes by ``id`` (UUID PKs aren't monotonic, so we page by timestamp).
    """
    from django.utils.dateparse import parse_datetime

    org = get_organization(request)
    conversation = get_object_or_404(
        InstagramConversation.objects.filter(organization=org), id=conversation_id,
    )

    messages_qs = conversation.messages.all()  # Meta.ordering = ['created_at']
    after = parse_datetime(request.GET.get('after', '') or '')
    if after is not None:
        messages_qs = messages_qs.filter(created_at__gte=after)

    hidden_inline = {
        InstagramMessage.STATUS_PENDING_REVIEW,
        InstagramMessage.STATUS_DISCARDED,
    }
    visible = [
        m for m in messages_qs
        if not (m.direction == InstagramMessage.DIRECTION_OUTBOUND
                and m.status in hidden_inline)
    ]

    pending_draft = (
        conversation.messages
        .filter(direction=InstagramMessage.DIRECTION_OUTBOUND,
                status=InstagramMessage.STATUS_PENDING_REVIEW)
        .order_by('-created_at')
        .first()
    )

    return JsonResponse({
        'messages': [_message_json(m, conversation) for m in visible],
        'status': conversation.status,
        'agent_in_control': conversation.agent_in_control,
        'pending_draft_id': str(pending_draft.id) if pending_draft else None,
    })


@login_required
@require_org
@require_admin
@require_instagram_dm_agent
@require_http_methods(["POST"])
def instagram_draft_approve(request, conversation_id, message_id):
    """Approve (optionally edited) or discard a queued draft."""
    org = get_organization(request)
    conversation = get_object_or_404(
        InstagramConversation.objects.filter(organization=org), id=conversation_id,
    )
    draft = get_object_or_404(
        InstagramMessage.objects.filter(
            conversation=conversation,
            direction=InstagramMessage.DIRECTION_OUTBOUND,
            status=InstagramMessage.STATUS_PENDING_REVIEW,
        ),
        id=message_id,
    )

    draft.reviewed_by = request.user
    draft.reviewed_at = timezone.now()

    if request.POST.get('action') == 'discard':
        draft.status = InstagramMessage.STATUS_DISCARDED
        draft.save(update_fields=['status', 'reviewed_by', 'reviewed_at'])
        messages.success(request, 'Draft discarded.')
        _maybe_resolve(conversation)
        return redirect('tickets:instagram_conversation_detail', conversation_id=conversation.id)

    edited = request.POST.get('content', '').strip()
    if edited:
        draft.content = edited

    send = get_sender(org).send_text(conversation.ig_user_id, draft.content, human_agent=True)
    if send.ok:
        draft.status = InstagramMessage.STATUS_APPROVED_SENT
        draft.provider_message_id = send.provider_message_id or draft.provider_message_id
        draft.save()
        messages.success(request, 'Reply sent.')
        _maybe_resolve(conversation)
    else:
        # D-note: a real sender surfaces a 24h-window / transport rejection here.
        # Keep the conversation awaiting_human so the organizer can retry — don't resolve.
        draft.status = InstagramMessage.STATUS_FAILED
        draft.escalation_reason = (send.error or draft.escalation_reason)[:300]
        draft.save()
        messages.error(request, f'Send failed: {send.error}')
    return redirect('tickets:instagram_conversation_detail', conversation_id=conversation.id)


@login_required
@require_org
@require_admin
@require_instagram_dm_agent
@require_http_methods(["POST"])
def instagram_message_send(request, conversation_id):
    """Send a free-form human reply; the human has taken over the thread."""
    org = get_organization(request)
    conversation = get_object_or_404(
        InstagramConversation.objects.filter(organization=org), id=conversation_id,
    )
    text = request.POST.get('content', '').strip()
    if not text:
        messages.error(request, 'Reply cannot be empty.')
        return redirect('tickets:instagram_conversation_detail', conversation_id=conversation.id)

    reply = InstagramMessage(
        conversation=conversation,
        direction=InstagramMessage.DIRECTION_OUTBOUND,
        author=InstagramMessage.AUTHOR_HUMAN,
        content=text,
        reviewed_by=request.user,
        reviewed_at=timezone.now(),
    )
    send = get_sender(org).send_text(conversation.ig_user_id, text, human_agent=True)
    update_fields = ['last_message_at']
    if send.ok:
        reply.status = InstagramMessage.STATUS_APPROVED_SENT
        reply.provider_message_id = send.provider_message_id or ''
        reply.save()
        # Human has taken over: supersede any still-pending AI drafts and keep the thread
        # human-owned (agent paused) until someone explicitly hands it back. The agent does
        # NOT resume on the next customer message — that's the whole point of the handoff.
        conversation.messages.filter(
            direction=InstagramMessage.DIRECTION_OUTBOUND,
            status=InstagramMessage.STATUS_PENDING_REVIEW,
        ).update(
            status=InstagramMessage.STATUS_DISCARDED,
            reviewed_by=request.user, reviewed_at=timezone.now(),
        )
        conversation.status = InstagramConversation.STATUS_HUMAN_HANDLING
        conversation.assigned_to = request.user
        update_fields.extend(['status', 'assigned_to'])
        messages.success(request, 'Reply sent.')
    else:
        reply.status = InstagramMessage.STATUS_FAILED
        reply.escalation_reason = (send.error or '')[:300]
        reply.save()
        messages.error(request, f'Send failed: {send.error}')
    conversation.last_message_at = timezone.now()
    conversation.save(update_fields=update_fields)
    return redirect('tickets:instagram_conversation_detail', conversation_id=conversation.id)


@login_required
@require_org
@require_admin
@require_instagram_dm_agent
@require_http_methods(["POST"])
def instagram_conversation_handback(request, conversation_id):
    """Hand a human-owned thread back to the AI agent.

    Clears human ownership and resolves the thread so the inbound task's handoff gate
    lets the agent answer future customer messages again. Any admin may hand back —
    ``assigned_to`` is informational, not an ownership lock.
    """
    org = get_organization(request)
    conversation = get_object_or_404(
        InstagramConversation.objects.filter(organization=org), id=conversation_id,
    )
    conversation.status = InstagramConversation.STATUS_RESOLVED
    conversation.assigned_to = None
    conversation.save(update_fields=['status', 'assigned_to'])
    # If the thread ends on a customer message the human never answered, let the now-
    # resumed agent pick it up — handing back shouldn't leave a question hanging.
    _resume_agent_on_pending_inbound(conversation, org)
    messages.success(request, 'Conversation handed back to the agent.')
    return redirect('tickets:instagram_conversation_detail', conversation_id=conversation.id)


def _resume_agent_on_pending_inbound(conversation, organization):
    """Re-run the inbound pipeline for a trailing unanswered customer message.

    Only when the last message in the thread is an inbound (no reply came after it). The
    task is idempotent — it dedups on provider_message_id and no-ops if a reply already
    exists or the agent is disabled — so this is safe. Skips a blank provider_message_id
    (can't dedup it, which would create a duplicate inbound). Enqueued on_commit so the
    worker reads the committed ``resolved`` status rather than racing the save.
    """
    from ..tasks import process_instagram_inbound_task

    last = conversation.messages.order_by('-created_at').first()
    if last is None or last.direction != InstagramMessage.DIRECTION_INBOUND:
        return
    if not last.provider_message_id:
        return
    normalized = {
        'ig_account_id': organization.instagram_business_account_id or '',
        'sender_id': conversation.ig_user_id,
        'text': last.content,
        'provider_message_id': last.provider_message_id,
        'timestamp': 0,
    }
    org_id = str(organization.id)
    transaction.on_commit(
        lambda: process_instagram_inbound_task.delay(org_id, normalized)
    )


@login_required
@require_org
@require_admin
@require_instagram_dm_agent
@require_http_methods(["POST"])
def instagram_conversation_resolve_close(request, conversation_id):
    """Resolve a human-owned thread AND reset the agent's context.

    Like hand-back, this re-enables the agent (status resolved, ownership cleared). Unlike
    hand-back, it stamps ``agent_context_reset_at`` so the agent treats future messages as
    a fresh conversation — it won't re-escalate a benign follow-up against an already-handled
    sensitive incident. It intentionally does NOT re-run the agent on a trailing message: the
    human is declaring the thread handled.
    """
    org = get_organization(request)
    conversation = get_object_or_404(
        InstagramConversation.objects.filter(organization=org), id=conversation_id,
    )
    conversation.status = InstagramConversation.STATUS_RESOLVED
    conversation.assigned_to = None
    conversation.agent_context_reset_at = timezone.now()
    conversation.save(update_fields=['status', 'assigned_to', 'agent_context_reset_at'])
    messages.success(
        request, 'Conversation resolved and closed. The agent will start fresh on new messages.')
    return redirect('tickets:instagram_conversation_detail', conversation_id=conversation.id)
