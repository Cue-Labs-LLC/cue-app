"""Fixed search-query plan for the Market Competition Agent (DO3).

The query set is a *pure function* of the event — no LLM, no network, no
agentic loop. Code builds a fixed, fully-testable list of search strings; the
LLM later does only the fuzzy work (extraction/normalization). Keeping the plan
deterministic means we can unit-test exactly which searches run.

See ``docs/technical-design/market-competition-agent.md`` §3, §4.2 (DO3).
"""
from django.conf import settings

DEFAULT_PLATFORMS = 'eventbrite,dice,seetickets'


def _platforms():
    """Configured non-API platform names seeded into the plan (ordered)."""
    raw = getattr(settings, 'MARKET_COMPETITION_PLATFORMS', DEFAULT_PLATFORMS)
    return [p.strip() for p in raw.split(',') if p.strip()]


def _event_date_label(event):
    """Readable label for the event's OWN date (never widened by the window).

    Every query carries the event's actual date so the search anchors on it
    directly. For a multi-day event the full span is used (``start``..``end``,
    DO5-b). The ±``MARKET_COMPETITION_DATE_WINDOW_DAYS`` proximity tolerance lives
    only in the scorer, not in the search text.
    """
    start = event.start_date
    end = event.end_date or event.start_date
    if start == end:
        # Single day: "June 15, 2024"
        return f'{start:%B} {start.day}, {start.year}'
    if start.year == end.year and start.month == end.month:
        # Same month: "June 15-20, 2024"
        return f'{start:%B} {start.day}-{end.day}, {start.year}'
    return f'{start:%B %d, %Y} to {end:%B %d, %Y}'


def build_queries(event, genre_hints) -> list:
    """Return the fixed, ordered list of web-search queries for ``event``.

    When genre hints are provided, **every** query carries the genre (DO3, pinned
    plan): one query per genre hint, then one per configured platform scoped to
    the genre(s) — there is no genre-less city-density query, because a known
    genre should never be dropped from a search.

    When there are no confident genre hints, the plan falls back to city+date
    density (DO5-f): a bare city query plus one per platform, all genre-less.

    Returns ``[]`` when the venue has no city (nothing locatable to search).
    """
    city = (getattr(event.venue, 'city', '') or '').strip()
    if not city:
        return []

    date_label = _event_date_label(event)
    hints = [h.strip() for h in (genre_hints or []) if h and h.strip()]
    queries = []

    if hints:
        for hint in hints:
            queries.append(f'{hint} events in {city} {date_label}')
        genre_phrase = ' '.join(hints)
        for platform in _platforms():
            queries.append(f'{platform} {genre_phrase} events in {city} {date_label}')
    else:
        queries.append(f'{city} events {date_label}')
        for platform in _platforms():
            queries.append(f'{platform} {city} events {date_label}')

    return queries
