"""Auto-detect a Typeform form's questions to ExternalSurveyResponse fields."""


# Valid ExternalSurveyResponse field names (target columns).
TARGET_FIELDS = [
    'overall_rating', 'nps_score', 'email', 'city',
    'enjoyed', 'genres', 'improvements',
    'crowd_vibe', 'venue_feel', 'pre_event_info', 'found_out_how',
    'text_feedback', 'raffle_email',
]

MULTI_SELECT_TARGETS = {'enjoyed', 'genres', 'improvements'}


def auto_field_map(form_definition: dict) -> dict:
    """Return {field.ref-or-id: target_field_name} for a Typeform form definition.

    Run after fetching the form via GET /forms/{id}. Org may edit the result.
    """
    mapping: dict[str, str] = {}
    used_targets: set[str] = set()
    email_seen = False
    fields = form_definition.get('fields') or []

    for field in fields:
        key = field.get('ref') or field.get('id')
        if not key:
            continue
        ftype = field.get('type') or ''
        title = (field.get('title') or '').lower()
        properties = field.get('properties') or {}
        is_multi = bool(properties.get('allow_multiple_selection'))

        target = None

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

        if target and target not in used_targets:
            mapping[key] = target
            used_targets.add(target)

    return mapping


def _any_in(haystack: str, needles: list[str]) -> bool:
    return any(needle in haystack for needle in needles)
