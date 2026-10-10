"""Fixed search-query plan for the Market Competition Agent (DO3).

The query set is a *pure function* of the event — no LLM, no network, no
agentic loop. Code builds a fixed, fully-testable list of search strings; the
LLM later does only the fuzzy work (extraction/normalization). Keeping the plan
deterministic means we can unit-test exactly which searches run.

See ``docs/technical-design/market-competition-agent.md`` §3, §4.2 (DO3).
"""
from datetime import timedelta

from django.conf import settings

DEFAULT_PLATFORMS = 'eventbrite,dice,seetickets'
DEFAULT_WINDOW_DAYS = 3


def _platforms():
    """Configured non-API platform names seeded into the plan (ordered)."""
    raw = getattr(settings, 'MARKET_COMPETITION_PLATFORMS', DEFAULT_PLATFORMS)
    return [p.strip() for p in raw.split(',') if p.strip()]


def _window_days():
    return getattr(settings, 'MARKET_COMPETITION_DATE_WINDOW_DAYS', DEFAULT_WINDOW_DAYS)


def _date_range(event, window):
    """Readable date-range label spanning the event's dates widened by ``window``.

    Uses the full date span (``start_date``..``end_date``) so a multi-day event's
    queries cover both ends (DO5-b), widened by the ±window.
    """
    start = event.start_date - timedelta(days=window)
    end = (event.end_date or event.start_date) + timedelta(days=window)
    if start.year == end.year and start.month == end.month:
        # Same month: "June 12-18, 2024"
        return f'{start:%B} {start.day}-{end.day}, {start.year}'
    return f'{start:%B %d, %Y} to {end:%B %d, %Y}'


def build_queries(event, genre_hints) -> list:
    """Return the fixed, ordered list of web-search queries for ``event``.

    Order (DO3 — pins the plan): one query per genre hint, then a city-density
    query, then one per configured platform. Returns ``[]`` when the venue has
    no city (nothing locatable to search).
    """
    city = (getattr(event.venue, 'city', '') or '').strip()
    if not city:
        return []

    date_range = _date_range(event, _window_days())
    queries = []

    for hint in genre_hints:
        hint = (hint or '').strip()
        if hint:
            queries.append(f'{hint} events in {city} {date_range}')

    queries.append(f'{city} events {date_range}')

    for platform in _platforms():
        queries.append(f'{platform} {city} events {date_range}')

    return queries
