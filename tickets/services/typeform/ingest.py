"""Convert a Typeform response payload into an ExternalSurveyResponse row."""

import logging

from django.utils.dateparse import parse_datetime

from tickets.services.typeform.field_mapping import MULTI_SELECT_TARGETS, TARGET_FIELDS


logger = logging.getLogger(__name__)


def ingest_response(subscription, form_response: dict):
    """Upsert one Typeform `form_response` into an ExternalSurveyResponse.

    Returns (response, created) tuple. Idempotent on (organization, typeform_response_id).
    """
    from tickets.models import ExternalSurveyResponse

    response_id = form_response.get('response_id') or form_response.get('token')
    submitted_at = parse_datetime(form_response.get('submitted_at') or '')
    if not response_id or not submitted_at:
        logger.warning('Typeform payload missing response_id or submitted_at; skipping.')
        return None, False

    answers = form_response.get('answers') or []
    definition_fields = (form_response.get('definition') or {}).get('fields') or []
    by_field_key = _index_definition(definition_fields)

    extracted: dict = {}
    for answer in answers:
        field = answer.get('field') or {}
        key = field.get('ref') or field.get('id')
        if not key:
            continue
        target = subscription.field_map.get(key)
        if not target or target not in TARGET_FIELDS:
            continue
        value = _field_value(answer, by_field_key.get(key, {}))
        if value is None:
            continue
        extracted[target] = value

    defaults = {
        'organization': subscription.organization,
        'upload': subscription.upload,
        'responded_at': submitted_at,
        'email': str(extracted.get('email') or '')[:254],
        'overall_rating': str(extracted.get('overall_rating') or '')[:30],
        'nps_score': _coerce_nps(extracted.get('nps_score')),
        'city': str(extracted.get('city') or '')[:100],
        'enjoyed': _as_list(extracted.get('enjoyed')),
        'genres': _as_list(extracted.get('genres')),
        'improvements': _as_list(extracted.get('improvements')),
        'crowd_vibe': str(extracted.get('crowd_vibe') or '')[:80],
        'venue_feel': str(extracted.get('venue_feel') or '')[:80],
        'pre_event_info': str(extracted.get('pre_event_info') or '')[:80],
        'found_out_how': str(extracted.get('found_out_how') or '')[:200],
        'text_feedback': str(extracted.get('text_feedback') or ''),
        'raffle_email': str(extracted.get('raffle_email') or '')[:254],
    }

    response, created = ExternalSurveyResponse.objects.update_or_create(
        organization=subscription.organization,
        typeform_response_id=response_id,
        defaults=defaults,
    )
    return response, created


def _index_definition(definition_fields: list[dict]) -> dict:
    out: dict = {}
    for field in definition_fields:
        ref = field.get('ref') or field.get('id')
        if ref:
            out[ref] = field
    return out


def _field_value(answer: dict, field_def: dict):
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
    return None


def _coerce_nps(value):
    if value is None or value == '':
        return None
    try:
        score = int(value)
    except (TypeError, ValueError):
        return None
    if 0 <= score <= 10:
        return score
    return None


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    if isinstance(value, str):
        return [s.strip() for s in value.split(',') if s.strip()]
    return [str(value)]
