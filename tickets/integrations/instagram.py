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
import uuid
from dataclasses import asdict
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import (
    Case, Count, IntegerField, OuterRef, Q, Subquery, Value, When,
)
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from ..forms import OrgFAQForm
from ..models import InstagramConversation, InstagramMessage, Organization, OrgFAQ
from ..services.instagram import get_sender
from ..utils import get_organization, require_admin, require_org

logger = logging.getLogger(__name__)


def _org_faqs(org):
    """Live (non-soft-deleted) FAQs for an org, in display order."""
    return OrgFAQ.objects.filter(organization=org, deleted_at__isnull=True)


def _is_ajax(request):
    return request.headers.get('x-requested-with') == 'XMLHttpRequest'


def require_instagram_feature(view_func):
    """404 the Instagram DM agent UX unless the org has the feature flag enabled.

    Master rollout gate: the code can ship to everyone while the whole UX (inbox, FAQ
    editor, settings) stays invisible until ``instagram_feature_enabled`` is turned on
    for a specific org. Applied to every UX view below — never the public webhook.
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        org = get_organization(request)
        if org is None or not org.instagram_feature_enabled:
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


@login_required
@require_org
@require_admin
@require_instagram_feature
def instagram_faq_list(request):
    """Inline, drag-and-drop editor for this org's support-agent FAQs."""
    org = get_organization(request)
    faqs = list(_org_faqs(org))
    return render(request, 'tickets/instagram_faq_list.html', {
        'faqs': faqs,
        'is_connected': bool(
            org.instagram_page_access_token and org.instagram_business_account_id
        ),
        'escalation_ack_text': org.instagram_escalation_ack_text,
    })


@login_required
@require_org
@require_admin
@require_instagram_feature
@require_http_methods(["POST"])
def instagram_agent_settings(request):
    """Save per-org support-agent settings (the escalation acknowledgement copy)."""
    org = get_organization(request)
    org.instagram_escalation_ack_text = request.POST.get('escalation_ack_text', '').strip()
    org.save(update_fields=['instagram_escalation_ack_text'])
    messages.success(request, 'Agent settings saved.')
    return redirect('tickets:instagram_faq_list')


@login_required
@require_org
@require_admin
@require_instagram_feature
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
@require_instagram_feature
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
@require_instagram_feature
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
@require_instagram_feature
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
@require_instagram_feature
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
@require_instagram_feature
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
@require_instagram_feature
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

    send = get_sender(org).send_text(conversation.ig_user_id, draft.content)
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
@require_instagram_feature
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
    send = get_sender(org).send_text(conversation.ig_user_id, text)
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
@require_instagram_feature
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
