"""Helpers for shaping Typeform survey responses for UI display.

These functions are pure (no DB access) so they're easy to unit-test and
reusable from both server-rendered templates (event_detail linked table)
and AJAX serializers (_survey_candidate_dict).
"""

from __future__ import annotations

from typing import Iterable


_PRIORITY_TEXT_TYPES = {'long_text', 'short_text'}


def _is_empty_value(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, tuple, dict)) and not value:
        return True
    return False


def enrich_answers_with_titles(raw_answers, questions):
    """Return raw_answers with `title` repopulated from the form's questions
    snapshot, keyed by ref → id. Drops nothing — UI decides what to hide.

    Typeform stores question titles on the form's `questions` array, not
    duplicated onto each answer. Without this enrichment, the UI falls
    back to "(untitled)" for any answer whose title field is empty.
    """
    lookup = {
        (q.get('ref') or q.get('id')): (q.get('title') or '').strip()
        for q in (questions or [])
        if isinstance(q, dict)
    }
    out = []
    for ans in (raw_answers or []):
        if not isinstance(ans, dict):
            out.append(ans)
            continue
        if not (ans.get('title') or '').strip():
            key = ans.get('ref') or ans.get('id')
            if key and lookup.get(key):
                ans = {**ans, 'title': lookup[key]}
        out.append(ans)
    return out


def pick_preview_pairs(answers, limit: int = 2):
    """Pick up to `limit` priority answers for compact preview rows.

    Priority order:
      1. opinion_scale (NPS)
      2. rating
      3. first non-empty long_text / short_text
      4. first remaining non-empty answer

    Returns a list of {title, value, type} dicts of length <= limit.
    Skips answers whose value is None / empty string / empty list.
    """
    if not answers:
        return []

    picked = []
    used_ids = set()

    def _id(ans):
        return id(ans)

    def _take(ans):
        if _id(ans) in used_ids:
            return False
        if _is_empty_value(ans.get('value')):
            return False
        picked.append({
            'title': ans.get('title') or '',
            'value': ans.get('value'),
            'type': ans.get('type') or '',
        })
        used_ids.add(_id(ans))
        return True

    # 1. opinion_scale (NPS) — first one wins
    for ans in answers:
        if not isinstance(ans, dict):
            continue
        if ans.get('type') == 'opinion_scale' and _take(ans):
            break
        if len(picked) >= limit:
            return picked

    # 2. rating
    if len(picked) < limit:
        for ans in answers:
            if not isinstance(ans, dict):
                continue
            if ans.get('type') == 'rating' and _take(ans):
                break
            if len(picked) >= limit:
                return picked

    # 3. first non-empty long/short text
    if len(picked) < limit:
        for ans in answers:
            if not isinstance(ans, dict):
                continue
            if ans.get('type') in _PRIORITY_TEXT_TYPES and _take(ans):
                break
            if len(picked) >= limit:
                return picked

    # 4. first remaining non-empty
    if len(picked) < limit:
        for ans in answers:
            if not isinstance(ans, dict):
                continue
            _take(ans)
            if len(picked) >= limit:
                return picked

    return picked
