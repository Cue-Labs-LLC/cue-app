"""Per-form field mapping: project raw Typeform answers into structured columns.

`auto_field_map` produces sensible defaults from a form definition (heuristic by
question type + title keywords). `apply_field_map` projects an ingested response's
`raw_answers` through the saved mapping into a dict of column updates suitable for
merging into the `ExternalSurveyResponse` defaults.
"""

from __future__ import annotations


# Target columns on ExternalSurveyResponse that orgs can map Typeform questions into.
TARGET_FIELDS: list[str] = [
    'email', 'overall_rating', 'nps_score', 'city',
    'enjoyed', 'genres', 'improvements',
    'crowd_vibe', 'venue_feel', 'pre_event_info', 'found_out_how',
    'text_feedback', 'raffle_email',
]

# Multi-select columns — values are lists of strings, not scalars.
MULTI_SELECT_TARGETS: set[str] = {'enjoyed', 'genres', 'improvements'}

# Typeform field types that wrap other fields rather than being answerable themselves.
GROUP_FIELD_TYPES: set[str] = {'group', 'inline_group'}

# Field types that don't produce a respondent answer and should be skipped entirely.
NON_ANSWERABLE_TYPES: set[str] = {'statement', 'thankyou_screen', 'welcome_screen'}


def flatten_form_fields(form_definition: dict) -> list[dict]:
    """Walk a Typeform form definition and return a flat list of leaf questions.

    Each returned dict is the raw Typeform field augmented with a `group_title`
    string (parent group's title, or '' for top-level questions). Container fields
    (group/inline_group) are recursed into via `field.properties.fields`; non-answerable
    types (statement, welcome/thankyou screens) are skipped.
    """
    out: list[dict] = []

    def walk(fields, parent_title=''):
        for field in fields or []:
            if not isinstance(field, dict):
                continue
            ftype = (field.get('type') or '').lower()
            if ftype in NON_ANSWERABLE_TYPES:
                continue
            if ftype in GROUP_FIELD_TYPES:
                sub = (field.get('properties') or {}).get('fields') or []
                walk(sub, parent_title=field.get('title') or parent_title)
                continue
            enriched = dict(field)
            enriched['group_title'] = parent_title
            out.append(enriched)

    walk((form_definition or {}).get('fields') or [])
    return out


def snapshot_questions(form_definition: dict) -> list[dict]:
    """Return a flat list of {id, ref, type, title, group_title} per leaf question.

    Walks `group`/`inline_group` containers and skips non-answerable types.
    Designed for persistence on `TypeformFormSubscription.questions` so the app
    has a reliable source of truth for question titles without re-fetching the
    form definition from Typeform on every render.
    """
    out: list[dict] = []
    for field in flatten_form_fields(form_definition):
        ref = field.get('ref') or ''
        fid = field.get('id') or ''
        if not (ref or fid):
            continue
        out.append({
            'id': fid,
            'ref': ref,
            'type': field.get('type') or '',
            'title': (field.get('title') or '').strip(),
            'group_title': field.get('group_title') or '',
        })
    return out


# CharField max_lengths on ExternalSurveyResponse. Kept here so apply_field_map
# can safely truncate without importing the model (avoids circular import in tests).
_MAX_LENGTHS: dict[str, int] = {
    'email': 254,
    'overall_rating': 30,
    'city': 100,
    'crowd_vibe': 80,
    'venue_feel': 80,
    'pre_event_info': 80,
    'found_out_how': 200,
    'raffle_email': 254,
}


def auto_field_map(form_definition: dict) -> dict:
    """Heuristic starting mapping from a `GET /forms/{id}` response.

    Returns `{field.ref or field.id: target_column}`. Each target column is
    assigned at most once — first match wins.
    """
    mapping: dict[str, str] = {}
    used: set[str] = set()
    email_seen = False
    fields = flatten_form_fields(form_definition)

    for field in fields:
        key = field.get('ref') or field.get('id')
        if not key:
            continue
        ftype = field.get('type') or ''
        title = (field.get('title') or '').lower()
        properties = field.get('properties') or {}
        is_multi = bool(properties.get('allow_multiple_selection'))
        target: str | None = None

        if ftype == 'rating':
            target = 'overall_rating'
        elif ftype == 'opinion_scale':
            steps = properties.get('steps') or 11
            if steps >= 10:
                target = 'nps_score'
        elif ftype == 'email':
            if not email_seen:
                target = 'email'
                email_seen = True
            elif _any_in(title, ['raffle', 'win', 'giveaway', 'prize']):
                target = 'raffle_email'
        elif ftype == 'short_text' and _any_in(title, ['city', 'town']):
            target = 'city'
        elif ftype == 'multiple_choice' and is_multi:
            if _any_in(title, ['genre', 'music style']):
                target = 'genres'
            elif _any_in(title, ['improve', 'better', 'change', 'wish']):
                target = 'improvements'
            elif _any_in(title, ['enjoy', 'love', 'liked', 'favorite']):
                target = 'enjoyed'
        elif ftype in ('multiple_choice', 'dropdown', 'picture_choice'):
            if _any_in(title, ['vibe', 'crowd', 'energy', 'atmosphere']):
                target = 'crowd_vibe'
            elif _any_in(title, ['venue', 'space', 'room']):
                target = 'venue_feel'
            elif _any_in(title, ['info', 'before', 'pre-event', 'pre event', 'communicat']):
                target = 'pre_event_info'
            elif _any_in(title, ['hear', 'found', 'discover', 'how did you']):
                target = 'found_out_how'
        elif ftype == 'long_text':
            target = 'text_feedback'

        if target and target not in used:
            mapping[key] = target
            used.add(target)

    return mapping


def apply_field_map(raw_answers: list, field_map: dict) -> dict:
    """Project a list of raw_answer dicts through a field_map into column updates.

    `raw_answers` is the JSONField shape stored at ingest time:
        [{'id': 'fid_x', 'ref': 'ref_x', 'type': 'rating', 'title': '...', 'value': ...}, ...]
    `field_map` is `{ref-or-id: target_column}`.

    Returns a dict of `{column_name: value}` ready to merge into the response's
    defaults. Empty `field_map` or `raw_answers` returns `{}`.
    """
    if not field_map or not raw_answers:
        return {}

    out: dict = {}
    for ans in raw_answers:
        if not isinstance(ans, dict):
            continue
        key = ans.get('ref') or ans.get('id')
        if not key:
            continue
        target = field_map.get(key)
        if not target or target not in TARGET_FIELDS:
            continue

        value = ans.get('value')
        if target == 'nps_score':
            out[target] = _coerce_nps(value)
        elif target in MULTI_SELECT_TARGETS:
            out[target] = _as_list(value)
        elif target == 'text_feedback':
            out[target] = '' if value is None else str(value)
        else:
            text = '' if value is None else str(value)
            max_len = _MAX_LENGTHS.get(target)
            out[target] = text[:max_len] if max_len else text

    return out


def _coerce_nps(value):
    if value is None or value == '':
        return None
    try:
        score = int(value)
    except (TypeError, ValueError):
        return None
    return score if 0 <= score <= 10 else None


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    if isinstance(value, str):
        return [s.strip() for s in value.split(',') if s.strip()]
    return [str(value)]


def _any_in(haystack: str, needles: list[str]) -> bool:
    return any(needle in haystack for needle in needles)
