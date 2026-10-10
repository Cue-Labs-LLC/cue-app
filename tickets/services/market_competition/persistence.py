"""Persistence + freshness for the Market Competition Agent (Phase 4).

Domain logic shared by the Celery task and the enqueue trigger
(``tickets.tasks``):

- ``compute_input_hash`` — a stable fingerprint of exactly the event fields a
  scan depends on (name, talent lineup, city/state, date span, ±window). DO5-b
  includes the full date span.
- ``is_competition_fresh`` — whether a persisted result still covers the event
  unchanged *and* is young enough to trust (DO4). The input hash suppresses
  re-scans only *within* the TTL window; past it, a re-scan is forced.
- ``persist_competition`` — write a :class:`CompetitionResult` onto the Event row
  (cache-on-model), mirroring ``EventSummaryService._persist_summary``. The input
  hash is written **only on a non-``unavailable`` scan** (D5 / D1=A): ``ready`` and
  ``inconclusive`` cache; ``unavailable`` leaves the hash empty so the next trigger
  re-runs. Token metering already happens inside the service calls — not here.

See ``docs/technical-design/market-competition-agent.md`` §4.1/§4.2, D5, DO4, DO5-b.
"""
import hashlib

from django.conf import settings
from django.utils import timezone


def compute_input_hash(event) -> str:
    """Return a sha256 fingerprint of the event data a competition scan depends on.

    Covers name, talent lineup names, venue city/state, the full date span
    (``start_date``..``end_date`` — DO5-b), and the ±window. A change in any of
    these makes a stored result stale; nothing else affects the scan.
    """
    venue = getattr(event, 'venue', None)
    parts = [
        event.name or '',
    ]
    try:
        parts.extend(t.name for t in event.talent_lineup.all())
    except Exception:
        # Unsaved event or no reverse manager — the scalar fields still fingerprint.
        pass
    parts.extend([
        getattr(venue, 'city', '') or '',
        getattr(venue, 'state', '') or '',
        event.start_date.isoformat() if event.start_date else '',
        event.end_date.isoformat() if event.end_date else '',
        str(settings.MARKET_COMPETITION_DATE_WINDOW_DAYS),
    ])
    payload = '\x1f'.join(parts)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def is_competition_fresh(event) -> bool:
    """True if a persisted scan still covers the event and is within the TTL (DO4).

    Requires a non-empty stored hash (an ``unavailable`` / never-scanned event has
    none), an exact match against the current input hash, and a
    ``competition_generated_at`` no older than ``MARKET_COMPETITION_RESULT_TTL_DAYS``.
    """
    if not event.competition_input_hash:
        return False
    if event.competition_input_hash != compute_input_hash(event):
        return False
    if not event.competition_generated_at:
        return False
    ttl_days = settings.MARKET_COMPETITION_RESULT_TTL_DAYS
    age = timezone.now() - event.competition_generated_at
    return age.days < ttl_days


def persist_competition(event, result) -> None:
    """Cache a :class:`CompetitionResult` onto the Event row (event_summary style).

    Always persists score/label/status/data/generated_at. Writes the input hash
    **only when the scan is not ``unavailable``** (D5 / D1=A) so failed scans
    re-run next time while ``ready``/``inconclusive`` results are suppressed within
    the TTL.
    """
    event.competition_score = result.score
    event.competition_label = result.label
    event.competition_status = result.status
    event.competition_data = result.to_dict()
    event.competition_generated_at = timezone.now()
    event.competition_input_hash = (
        compute_input_hash(event) if result.status != 'unavailable' else ''
    )
    event.save(update_fields=[
        'competition_score',
        'competition_label',
        'competition_status',
        'competition_data',
        'competition_generated_at',
        'competition_input_hash',
    ])
