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

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from ..forms import OrgFAQForm
from ..models import Organization, OrgFAQ
from ..utils import get_organization, require_admin, require_org

logger = logging.getLogger(__name__)


def _org_faqs(org):
    """Live (non-soft-deleted) FAQs for an org, in display order."""
    return OrgFAQ.objects.filter(organization=org, deleted_at__isnull=True)


def _is_ajax(request):
    return request.headers.get('x-requested-with') == 'XMLHttpRequest'


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
