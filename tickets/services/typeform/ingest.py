"""Convert a Typeform response payload into an ExternalSurveyResponse row.

We store the full answer payload verbatim in `raw_answers`. The LLM event matcher
reads from this blob. If the subscription has a `field_map` configured, the
matching answers are also projected into the structured ExternalSurveyResponse
columns (overall_rating, nps_score, city, etc.) via `apply_field_map`.
"""

import logging

from django.utils.dateparse import parse_datetime

from tickets.services.typeform.field_mapping import apply_field_map, flatten_form_fields


logger = logging.getLogger(__name__)


def ingest_response(subscription, form_response: dict):
    """Upsert one Typeform `form_response` into an ExternalSurveyResponse.

    Returns (response, created). Idempotent on (organization, typeform_response_id).
    """
    from tickets.models import ExternalSurveyResponse

    response_id = form_response.get('response_id') or form_response.get('token')
    submitted_at = parse_datetime(form_response.get('submitted_at') or '')
    if not response_id or not submitted_at:
        logger.warning('Typeform payload missing response_id or submitted_at; skipping.')
        return None, False

    definition_fields = (form_response.get('definition') or {}).get('fields') or []
    titles_by_key = _index_titles(definition_fields)
    # Webhook payloads don't always include a full definition.fields list, and
    # even when they do, group children may be missing. Layer the persisted
    # subscription snapshot underneath so leaf-question titles still resolve.
    for q in (subscription.questions or []):
        if not isinstance(q, dict):
            continue
        key = q.get('ref') or q.get('id')
        if key and not titles_by_key.get(key):
            title = (q.get('title') or '').strip()
            if title:
                titles_by_key[key] = title

    raw_answers = []
    for answer in form_response.get('answers') or []:
        field = answer.get('field') or {}
        field_id = field.get('id') or ''
        ref = field.get('ref') or ''
        atype = answer.get('type') or field.get('type') or ''
        key = ref or field_id
        title = titles_by_key.get(key) or field.get('title') or ''
        raw_answers.append({
            'id': field_id,
            'ref': ref,
            'type': atype,
            'title': title,
            'value': _field_value(answer),
        })

    defaults = {
        'upload': subscription.upload,
        'responded_at': submitted_at,
        'raw_answers': raw_answers,
    }
    # Project mapped columns when the subscription has a field_map configured.
    defaults.update(apply_field_map(raw_answers, subscription.field_map or {}))

    response, created = ExternalSurveyResponse.objects.update_or_create(
        organization=subscription.organization,
        typeform_response_id=response_id,
        defaults=defaults,
    )
    return response, created


def apply_field_map_to_subscription(subscription, new_map: dict, limit: int = 500) -> tuple[int, set[str]]:
    """Re-project ``new_map`` over the most recent ``limit`` responses for ``subscription``.

    Also repairs missing/empty answer titles in ``raw_answers`` using the persisted
    ``subscription.questions`` snapshot. Uses ``QuerySet.update()`` so signals are
    bypassed; the caller (or this helper's caller) is responsible for invalidating
    any downstream event-stats / upload-stats caches via the returned event IDs.

    Returns ``(backfilled_count, affected_event_ids)``.
    """
    from tickets.models import ExternalSurveyResponse

    if not subscription.upload_id:
        return 0, set()

    title_lookup = {
        (q.get('ref') or q.get('id')): (q.get('title') or '').strip()
        for q in (subscription.questions or [])
        if isinstance(q, dict)
    }
    qs = (
        ExternalSurveyResponse.objects
        .filter(organization=subscription.organization, upload_id=subscription.upload_id)
        .exclude(typeform_response_id='')
        .order_by('-responded_at')[:limit]
    )

    backfilled = 0
    affected_event_ids: set[str] = set()
    for resp in qs:
        update = apply_field_map(resp.raw_answers or [], new_map)

        fixed_raw = []
        titles_changed = False
        for ans in (resp.raw_answers or []):
            if not isinstance(ans, dict):
                fixed_raw.append(ans)
                continue
            key = ans.get('ref') or ans.get('id')
            correct = title_lookup.get(key) if key else ''
            if correct and (ans.get('title') or '') != correct:
                ans = {**ans, 'title': correct}
                titles_changed = True
            fixed_raw.append(ans)
        if titles_changed:
            update['raw_answers'] = fixed_raw

        if update:
            ExternalSurveyResponse.objects.filter(pk=resp.pk).update(**update)
            if resp.event_id:
                affected_event_ids.add(str(resp.event_id))
            backfilled += 1

    return backfilled, affected_event_ids


def _index_titles(definition_fields: list[dict]) -> dict:
    """Build {field-key → title} from a webhook payload's `definition.fields`.

    Walks `group`/`inline_group` containers via `flatten_form_fields` so leaf
    questions nested inside a group still get their titles indexed.
    """
    if not definition_fields:
        return {}
    out: dict = {}
    for field in flatten_form_fields({'fields': definition_fields}):
        key = field.get('ref') or field.get('id')
        if key:
            out[key] = (field.get('title') or '').strip()
    return out


def _field_value(answer: dict):
    """Read the answer's value in whatever shape Typeform sent it."""
    atype = answer.get('type')
    if atype == 'text':
        return answer.get('text')
    if atype == 'email':
        return answer.get('email')
    if atype == 'number':
        return answer.get('number')
    if atype == 'boolean':
        return 'Yes' if answer.get('boolean') else 'No'
    if atype == 'date':
        return answer.get('date')
    if atype == 'choice':
        choice = answer.get('choice') or {}
        return choice.get('label') or choice.get('other')
    if atype == 'choices':
        choices = answer.get('choices') or {}
        labels = list(choices.get('labels') or [])
        if choices.get('other'):
            labels.append(choices['other'])
        return labels
    if atype == 'url':
        return answer.get('url')
    if atype == 'file_url':
        return answer.get('file_url')
    if atype == 'payment':
        payment = answer.get('payment') or {}
        return payment.get('amount')
    if atype == 'phone_number':
        return answer.get('phone_number')
    return None
