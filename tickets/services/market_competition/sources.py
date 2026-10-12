"""Competitor-event sources (D1=B — the interface a 2nd real source triggers).

A ``CompetitorEventSource`` turns a target event into a :class:`ScanResult` (the
same shape the scorer already consumes). The calculator fans out over the
configured active sources and merges their results, so adding a source later is a
drop-in: implement the ABC, register it, add its name to
``MARKET_COMPETITION_SOURCES``.

Shipping sources:
  - ``PoshVipSource`` (``'posh'``) — the posh.vip Apify scraper (default active).
    Enumerable, structured nightlife data; LLM-free (keyword genre + the feed's
    own location scoping as the metro). Catches the long-tail club events a ranked
    web-search index misses.
  - ``WebSearchSource`` (``'web'``) — the original Tavily + LLM-extraction scanner,
    kept behind the ABC but inactive by default. Re-enable via the setting.

See ``docs/technical-design/market-competition-agent.md`` §3/§4.2, D1=B.
"""
import logging
from abc import ABC, abstractmethod
from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from . import scanner
from .apify_client import fetch_posh_events
from .scanner import ScanResult, _dedupe, _is_target, _norm
from .types import CompetitorEvent

logger = logging.getLogger(__name__)

_DEFAULT_TZ = 'America/Los_Angeles'


def _posh_local_date(raw, tz_name):
    """Local calendar date of a posh ISO datetime, in the event's own timezone.

    posh returns ``startDateTime`` in **UTC** (e.g. ``2026-10-24T04:00:00.000Z``);
    a late LA show at 04:00 UTC is really the night of Oct 23 local. Truncating the
    UTC string would wrongly bump it to Oct 24, so we convert to the event's
    ``timezone`` first, then take the date. Falls back gracefully for odd inputs.
    """
    if not raw:
        return None
    s = str(raw).strip().replace('Z', '+00:00')
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            return date.fromisoformat(str(raw).strip()[:10])
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo('UTC'))
    try:
        dt = dt.astimezone(ZoneInfo(tz_name or _DEFAULT_TZ))
    except Exception:
        pass
    return dt.date()


def _coverage(hits, *, total=1, events=0):
    """A coverage dict in the shape the scorer/calculator expect."""
    return {
        'queries_total': total,
        'queries_with_results': hits,
        'ratio': (hits / total) if total else 0.0,
        'events_total': events,
    }


class CompetitorEventSource(ABC):
    """One source of competing events. Must never raise — fail to ``ok=False``."""

    name = ''

    @abstractmethod
    def fetch(self, organization, event, genre_hints, *,
              include_domains=None, exclude_domains=None) -> ScanResult:
        ...


class WebSearchSource(CompetitorEventSource):
    """Tavily web search + one LLM extraction pass (the original scanner)."""

    name = 'web'

    def fetch(self, organization, event, genre_hints, *,
              include_domains=None, exclude_domains=None) -> ScanResult:
        if not getattr(settings, 'TAVILY_API_KEY', ''):
            logger.warning(
                "Market competition web source unavailable: TAVILY_API_KEY unset"
            )
            return ScanResult(ok=False, coverage=_coverage(0, total=0))
        return scanner.scan_competitors(
            organization, event, genre_hints,
            include_domains=include_domains, exclude_domains=exclude_domains,
        )


def _when_for_date(start_date):
    """Map a target date to posh's only discovery windows (Today/This Week/This Month)."""
    today = timezone.localdate()
    days = (start_date - today).days
    if days <= 7:
        return 'This Week'
    return 'This Month'


class PoshVipSource(CompetitorEventSource):
    """posh.vip nightlife events via the hypebridge/posh-vip Apify actor.

    The explore feed is scoped to one city, so the whole feed is the target metro
    (no per-event metro normalization). Genre is keyword-inferred per event (no
    LLM) — weak on branded titles, so the scorer leans on metro+date density. If
    the feed doesn't reach the target date (event beyond posh's ~1-month horizon),
    an empty result is reported as zero coverage, not a false "you're clear".
    """

    name = 'posh'

    def fetch(self, organization, event, genre_hints, *,
              include_domains=None, exclude_domains=None) -> ScanResult:
        from .calculator import genres_in_text  # lazy: avoid import cycle

        venue = getattr(event, 'venue', None)
        city = (getattr(venue, 'city', '') or '').strip() if venue else ''
        if not city:
            return ScanResult(ok=False, coverage=_coverage(0, total=0))

        items = fetch_posh_events(city, _when_for_date(event.start_date))
        if items is None:  # no token / provider error — unavailable (D2)
            return ScanResult(ok=False, coverage=_coverage(0, total=0))

        metro = _norm(city)
        default_tz = getattr(event, 'timezone', '') or _DEFAULT_TZ
        competitors = []
        feed_dates = []
        for it in items:
            name = (it.get('eventTitle') or '').strip()
            if not name:
                continue
            # Convert posh's UTC startDateTime to the event's local date (the UTC
            # calendar date is a day ahead for late-night shows).
            d = _posh_local_date(it.get('startDateTime'), it.get('timezone') or default_tz)
            if d:
                feed_dates.append(d)
            genre = ' '.join(
                genres_in_text(f"{name} {it.get('description') or ''}")
            )
            competitors.append(CompetitorEvent(
                name=name,
                date=d,
                venue_name=(it.get('venueName') or ''),
                metro_normalized=metro,
                genre=genre,
                platform='posh',
                source_url=(it.get('eventUrl') or ''),
            ))

        competitors = [c for c in competitors if not _is_target(event, c)]  # DO5-a
        competitors = _dedupe(competitors)  # DO5-c

        feed_max = max(feed_dates) if feed_dates else None
        reaches = feed_max is not None and feed_max >= event.start_date
        return ScanResult(
            competitors=competitors,
            target_metro_normalized=metro,
            coverage=_coverage(1 if reaches else 0, events=len(items)),
            ok=True,
        )


_REGISTRY = {
    'web': WebSearchSource,
    'posh': PoshVipSource,
}


def get_active_sources():
    """Instantiate the sources named in ``MARKET_COMPETITION_SOURCES`` (default posh)."""
    raw = getattr(settings, 'MARKET_COMPETITION_SOURCES', 'posh') or 'posh'
    names = [n.strip().lower() for n in raw.split(',') if n.strip()]
    out = []
    for n in names:
        cls = _REGISTRY.get(n)
        if cls is None:
            logger.warning("Unknown market competition source %r (skipped)", n)
            continue
        out.append(cls())
    return out or [PoshVipSource()]


def fetch_competitors(organization, event, genre_hints, *,
                      include_domains=None, exclude_domains=None) -> ScanResult:
    """Fan out over the active sources and merge into one :class:`ScanResult`.

    Unions the competitor lists, dedupes across sources (DO5-c), aggregates
    coverage, and sets ``ok = any source succeeded`` — so one source failing never
    takes the scan down while another has data. With a single active source this
    is a pass-through.
    """
    all_competitors = []
    metro = ''
    any_ok = False
    cov_total = cov_hits = 0

    for source in get_active_sources():
        result = source.fetch(
            organization, event, genre_hints,
            include_domains=include_domains, exclude_domains=exclude_domains,
        )
        if result.ok:
            any_ok = True
        all_competitors.extend(result.competitors)
        if result.target_metro_normalized and not metro:
            metro = result.target_metro_normalized
        cov = result.coverage or {}
        cov_total += cov.get('queries_total', 0)
        cov_hits += cov.get('queries_with_results', 0)

    all_competitors = _dedupe(all_competitors)  # collapse the same show across sources
    ratio = (cov_hits / cov_total) if cov_total else (1.0 if any_ok else 0.0)
    return ScanResult(
        competitors=all_competitors,
        target_metro_normalized=metro,
        coverage={
            'queries_total': cov_total,
            'queries_with_results': cov_hits,
            'ratio': ratio,
        },
        ok=any_ok,
    )
