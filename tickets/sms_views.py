"""Views for native marketing SMS: campaigns, recipient lists, and Twilio webhooks.

Kept out of the (very large) views.py for cohesion. Every authenticated view is
org-scoped and gated behind the per-org ``sms_marketing_enabled`` flag.
"""

import logging
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import JsonResponse, HttpResponse, Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods

from .models import (
    SMSCampaign, SMSRecipientList, SMSMessageRecipient, PhoneSuppression,
)
from .forms import SMSCampaignForm, SMSRecipientListForm
from .sms import normalize_phone, validate_twilio_request, sms_segment_info, send_sms
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
    return qs.annotate(
        sent_count=Count('recipients', filter=Q(recipients__status__in=_HANDED_OFF)),
        delivered_count=Count('recipients', filter=Q(recipients__status='delivered')),
        failed_count=Count('recipients', filter=Q(recipients__status__in=_FAILED)),
    )


def _criteria_from_post(post):
    """Build a filter_criteria dict + manual id lists from raw POST data (used by
    the live preview endpoint, which fires before the list is saved)."""
    criteria = {}
    segments = [s for s in post.getlist('rfm_segment') if s]
    if segments:
        criteria['rfm_segment'] = segments
    tag_ids = [t for t in post.getlist('tag_ids') if t]
    if tag_ids:
        criteria['tag_ids'] = tag_ids
    min_ltv = (post.get('min_ltv') or '').strip()
    if min_ltv:
        criteria['min_ltv'] = min_ltv
    last_order_after = (post.get('last_order_after') or '').strip()
    if last_order_after:
        criteria['last_order_after'] = last_order_after
    includes = [s.strip() for s in (post.get('manual_include_ids') or '').split(',') if s.strip()]
    excludes = [s.strip() for s in (post.get('manual_exclude_ids') or '').split(',') if s.strip()]
    return criteria, includes, excludes


# ---------------------------------------------------------------------------
# Recipient lists
# ---------------------------------------------------------------------------

@login_required
@require_org
@require_host
@require_sms_feature
def sms_recipient_list_list(request):
    org = get_organization(request)
    lists = SMSRecipientList.objects.filter(
        organization=org, deleted_at__isnull=True,
    ).order_by('-created_at')
    return render(request, 'tickets/marketing/sms/recipient_list_list.html', {'lists': lists})


@login_required
@require_org
@require_host
@require_sms_feature
@require_http_methods(['GET', 'POST'])
def sms_recipient_list_create(request):
    org = get_organization(request)
    if request.method == 'POST':
        form = SMSRecipientListForm(request.POST, organization=org)
        if form.is_valid():
            recipient_list = form.save()
            messages.success(request, f'Recipient list "{recipient_list.name}" created.')
            return redirect('tickets:sms_recipient_list_detail', pk=recipient_list.id)
    else:
        form = SMSRecipientListForm(organization=org)
    return render(request, 'tickets/marketing/sms/recipient_list_form.html', {'form': form})


@login_required
@require_org
@require_host
@require_sms_feature
def sms_recipient_list_detail(request, pk):
    org = get_organization(request)
    recipient_list = get_object_or_404(
        SMSRecipientList.objects.filter(organization=org, deleted_at__isnull=True), id=pk,
    )
    cap = getattr(settings, 'SMS_CAMPAIGN_MAX_RECIPIENTS', 5000)
    recipients = recipient_list.materialize(org, cap=cap + 1)
    return render(request, 'tickets/marketing/sms/recipient_list_detail.html', {
        'recipient_list': recipient_list,
        'recipient_count': min(len(recipients), cap),
        'exceeds_cap': len(recipients) > cap,
        'cap': cap,
    })


@login_required
@require_org
@require_host
@require_sms_feature
@require_POST
def sms_recipient_list_preview(request):
    """JSON: resolved recipient count for the audience being built (live sizing)."""
    org = get_organization(request)
    criteria, includes, excludes = _criteria_from_post(request.POST)
    cap = getattr(settings, 'SMS_CAMPAIGN_MAX_RECIPIENTS', 5000)
    if not criteria and not includes:
        return JsonResponse({'count': 0, 'exceeds_cap': False, 'cap': cap})
    tmp = SMSRecipientList(
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
# Campaigns
# ---------------------------------------------------------------------------

@login_required
@require_org
@require_host
@require_sms_feature
def sms_campaign_list(request):
    org = get_organization(request)
    campaigns = _annotate_counts(
        SMSCampaign.objects.filter(organization=org, deleted_at__isnull=True)
        .select_related('recipient_list')
    ).order_by('-created_at')
    paginator = Paginator(campaigns, 25)
    page_obj = paginator.get_page(request.GET.get('page'))
    return render(request, 'tickets/marketing/sms/campaign_list.html', {'page_obj': page_obj})


@login_required
@require_org
@require_host
@require_sms_feature
@require_http_methods(['GET', 'POST'])
def sms_campaign_create(request):
    """Compose + send/schedule. A valid POST first shows a confirmation panel with
    the resolved recipient count; only a second POST with `confirm` dispatches."""
    org = get_organization(request)
    cap = getattr(settings, 'SMS_CAMPAIGN_MAX_RECIPIENTS', 5000)
    confirm_count = None
    exceeds_cap = False

    if request.method == 'POST':
        form = SMSCampaignForm(request.POST, organization=org)
        if form.is_valid():
            recipient_list = form.cleaned_data['recipient_list']
            recipients = recipient_list.materialize(org, cap=cap + 1)
            confirm_count = len(recipients)
            exceeds_cap = confirm_count > cap

            if exceeds_cap:
                form.add_error(
                    'recipient_list',
                    f'This audience resolves to more than {cap} recipients. '
                    f'Narrow the list before sending.',
                )
            elif confirm_count == 0:
                form.add_error('recipient_list', 'This audience has no contactable recipients.')
            elif request.POST.get('confirm'):
                scheduled = form.cleaned_data.get('send_mode') == SMSCampaignForm.SEND_SCHEDULE
                campaign = form.save(commit=False)
                campaign.organization = org
                campaign.created_by = request.user
                if scheduled:
                    campaign.status = SMSCampaign.Status.SCHEDULED
                    campaign.scheduled_at = form.cleaned_data['scheduled_at']
                    campaign.save()
                    messages.success(
                        request,
                        f'Campaign scheduled for {campaign.scheduled_at:%b %d, %Y %I:%M %p}.',
                    )
                else:
                    campaign.status = SMSCampaign.Status.DRAFT
                    campaign.save()
                    send_sms_campaign_task.delay(str(campaign.id))
                    messages.success(request, f'Sending "{campaign.name}" to {confirm_count} recipients.')
                return redirect('tickets:sms_campaign_detail', pk=campaign.id)
    else:
        form = SMSCampaignForm(organization=org)

    encoding, segments = sms_segment_info(request.POST.get('body', '') if request.method == 'POST' else '')
    return render(request, 'tickets/marketing/sms/campaign_form.html', {
        'form': form,
        'confirm_count': confirm_count,
        'exceeds_cap': exceeds_cap,
        'cap': cap,
        'preview_encoding': encoding,
        'preview_segments': segments,
    })


@login_required
@require_org
@require_host
@require_sms_feature
def sms_campaign_detail(request, pk):
    org = get_organization(request)
    campaign = get_object_or_404(
        _annotate_counts(
            SMSCampaign.objects.filter(organization=org, deleted_at__isnull=True)
            .select_related('recipient_list')
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
        'page_obj': page_obj,
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
        messages.success(request, 'Scheduled campaign canceled.')
    else:
        messages.error(request, 'That campaign can no longer be canceled.')
    return redirect('tickets:sms_campaign_detail', pk=pk)


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
    elif from_phone and opt_out_type == 'START':
        PhoneSuppression.objects.filter(phone=from_phone, organization__isnull=True).delete()
    # HELP / normal inbound → no-op (Twilio replies). Empty TwiML.
    return HttpResponse('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                        content_type='text/xml')
