"""LLM scanner for the Market Competition Agent (Phase 3).

Turns the fixed query plan's raw, unstructured web-search snippets into a clean
list of normalized ``CompetitorEvent`` rows. This is the one place in the
pipeline where the LLM earns its keep (DO3): the query set and the scorer are
deterministic; the *fuzzy* work — reading messy snippets, pinning dates,
normalizing metros, naming genres/platforms — is a single structured-output
extraction call (pattern mirrored from ``sms_strategist.py`` and
``marketing/ai_narrative.py``).

The scanner also enforces the DO5 guarantees deterministically **in code**
(not on LLM trust) so they are unit-testable regression classes:
  * DO5-a self-exclusion — the target event is never its own competitor.
  * DO5-c cross-platform dedupe — the same show on three sites collapses to one.
  * DO5-d undated handling — a blank/unparseable date becomes ``date=None`` and
    the scorer surfaces it separately.
  * DO5-e metro normalization — the LLM emits a normalized metro for every
    competitor *and* for the target (``target_metro_normalized``) in the same
    pass, so the scorer compares like-for-like without a hand-maintained map.

See ``docs/technical-design/market-competition-agent.md`` §4.2 (scanner), DO3,
DO5-a/c/d/e.
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import date
from itertools import zip_longest
from typing import Optional

from django.conf import settings
from pydantic import BaseModel, Field

from tickets.models import AITokenUsage
from tickets.services.ai_metering import record_ai_token_usage
from tickets.services.ai_tracing import trace_config

from .query_plan import build_queries
from .search_client import search_queries
from .types import CompetitorEvent

logger = logging.getLogger(__name__)

# Keep the single extraction prompt bounded regardless of how many results the
# search layer returns (DO5-g — per-scan cost is fixed by design).
MAX_SNIPPETS = 60
SNIPPET_CONTENT_CHARS = 400

EXTRACTION_SYSTEM_PROMPT = (
    "You extract structured event listings from noisy web-search snippets for a "
    "competitive-market analysis. You are given a TARGET event and a batch of "
    "search results. Return every DISTINCT real event you can find in the "
    "results that could compete for the same audience in the same metro area.\n"
    "Rules:\n"
    "- Exclude the TARGET event itself — do not list it as a competitor.\n"
    "- Collapse the same event appearing on multiple ticketing sites into ONE "
    "entry.\n"
    "- Only set a date you are confident about (ISO yyyy-mm-dd); otherwise leave "
    "it null. Never guess a date.\n"
    "- metro_normalized: a lowercase canonical 'city, state' for the event's "
    "location (e.g. 'los angeles, ca'). Use the SAME scheme for every event and "
    "for target_metro_normalized.\n"
    "- Do not invent events, venues, dates, or URLs. Skip non-events (articles, "
    "venue home pages, listicles with no concrete show)."
)


class ExtractedEvent(BaseModel):
    """One event the LLM pulled out of the search snippets."""
    name: str = Field(description="The event / show / concert name.")
    date: Optional[str] = Field(
        default=None,
        description="Event date as ISO yyyy-mm-dd, or null if not confidently known.",
    )
    venue_name: str = Field(default='', description="Venue the event is at, if stated.")
    metro_normalized: str = Field(
        default='',
        description="Canonical lowercase 'city, state' for this event (e.g. 'los angeles, ca').",
    )
    genre: str = Field(default='', description="Music/event genre or category, if evident.")
    platform: str = Field(
        default='',
        description="Where it is listed (eventbrite / dice / see tickets / venue box office / ...).",
    )
    source_url: str = Field(default='', description="The result URL this event came from.")


class ExtractedEvents(BaseModel):
    """The structured-output envelope for one extraction pass."""
    target_metro_normalized: str = Field(
        default='',
        description=(
            "Normalized metro for the TARGET event's city, in the SAME scheme as "
            "each event's metro_normalized (lowercase 'city, state')."
        ),
    )
    events: list[ExtractedEvent] = Field(
        default_factory=list,
        description="Every distinct competing event found; exclude the target event itself.",
    )


@dataclass
class ScanResult:
    """What a scan produced for the calculator to score.

    ``ok`` is False only when the extraction call itself failed (missing/invalid
    OpenAI key, provider error, unparseable output) — the calculator maps that to
    an ``unavailable`` status (D2), distinct from a genuine empty-but-successful
    scan (which the scorer may report as ``ready`` 0 or ``inconclusive``).
    """
    competitors: list = field(default_factory=list)
    target_metro_normalized: str = ''
    coverage: dict = field(default_factory=dict)
    ok: bool = True


def _norm(value):
    return (value or '').strip().lower()


def _parse_date(raw):
    """ISO date string -> date, or None (blank/invalid => undated, DO5-d)."""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
    except (ValueError, AttributeError):
        return None


def _interleave(results_by_query):
    """Yield results round-robin across queries: 1st of each, then 2nd of each, ...

    Fairly represents every query within the ``MAX_SNIPPETS`` budget. A plain
    sequential flatten drains the first queries and starves the later ones, so a
    low-volume but on-target query (e.g. a platform-specific search) can be
    truncated out of the payload entirely once earlier queries overflow the cap.
    Round-robin takes each query's *best* hits first and keeps the pool balanced
    regardless of how many queries the plan issued.
    """
    for tier in zip_longest(*results_by_query.values()):
        for r in tier:
            if r is not None:
                yield r


def _build_payload(event, results_by_query):
    """Compact JSON payload: the target context + a capped, flattened snippet list."""
    venue = event.venue
    end = event.end_date or event.start_date
    snippets = []
    seen_urls = set()
    for r in _interleave(results_by_query):
        url = r.get('url', '')
        # Cheap pre-dedupe on identical URLs keeps the prompt small; the real
        # fuzzy dedupe happens after extraction.
        if url and url in seen_urls:
            continue
        seen_urls.add(url)
        content = (r.get('content') or '')[:SNIPPET_CONTENT_CHARS]
        snippets.append({
            'title': r.get('title', ''),
            'url': url,
            'content': content,
        })
        if len(snippets) >= MAX_SNIPPETS:
            break

    return {
        'target_event': {
            'name': event.name,
            'city': getattr(venue, 'city', '') or '',
            'state': getattr(venue, 'state', '') or '',
            'venue_name': getattr(venue, 'name', '') or '',
            'start_date': event.start_date.isoformat(),
            'end_date': end.isoformat(),
        },
        'search_results': snippets,
    }


def _is_target(event, competitor):
    """DO5-a: True when ``competitor`` is (a dedupe of) the target event itself."""
    if _norm(competitor.name) != _norm(event.name):
        return False
    venue_match = (
        _norm(competitor.venue_name)
        and _norm(competitor.venue_name) == _norm(getattr(event.venue, 'name', ''))
    )
    span_start = event.start_date
    span_end = event.end_date or event.start_date
    date_match = competitor.date is not None and span_start <= competitor.date <= span_end
    return bool(venue_match or date_match)


def _dedupe(competitors):
    """DO5-c: collapse the same show across platforms by a fuzzy name+date+venue key."""
    seen = set()
    out = []
    for c in competitors:
        key = (_norm(c.name), c.date.isoformat() if c.date else '', _norm(c.venue_name))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def scan_competitors(organization, event, genre_hints, *,
                     include_domains=None, exclude_domains=None) -> ScanResult:
    """Run the fixed query plan + one LLM extraction pass into ``CompetitorEvent`` rows.

    Records its own ``extract``-stage token usage (each call site owns its
    metering, like ``event_summary``). Returns a :class:`ScanResult`; never
    raises — an extraction failure surfaces as ``ok=False`` for the calculator to
    map to ``unavailable`` (D2).

    ``include_domains`` / ``exclude_domains`` scope the web searches (allow-list /
    deny-list of hostnames) and are forwarded to the search layer. ``None`` keeps
    the search layer's own default; pass a list to override for this scan.
    """
    queries = build_queries(event, genre_hints)
    if not queries:
        return ScanResult(
            competitors=[],
            target_metro_normalized='',
            coverage={'queries_total': 0, 'queries_with_results': 0, 'ratio': 1.0},
            ok=True,
        )

    results_by_query, coverage = search_queries(
        queries, include_domains=include_domains, exclude_domains=exclude_domains,
    )
    payload = _build_payload(event, results_by_query)

    from langchain_openai import ChatOpenAI

    model_name = getattr(settings, 'OPENAI_MODEL', 'gpt-4o')
    user_content = (
        "Extract competing events from these search results. Use only the data "
        "provided — do not invent events, dates, or URLs.\n\n"
        + json.dumps(payload, default=str)
    )

    try:
        llm = ChatOpenAI(
            model=model_name,
            api_key=getattr(settings, 'OPENAI_API_KEY', ''),
            temperature=0.2,
            stream_usage=True,
        )
        structured_llm = llm.with_structured_output(ExtractedEvents, include_raw=True)
        raw_result = structured_llm.invoke(
            [
                {'role': 'system', 'content': EXTRACTION_SYSTEM_PROMPT},
                {'role': 'user', 'content': user_content},
            ],
            **trace_config(
                name='market-competition-extract',
                tags=['market-competition', 'extract'],
                metadata={'event_id': str(event.id)},
            ),
        )
    except Exception as exc:
        logger.warning(
            "Market competition extraction failed for event %s: %s", event.id, exc
        )
        return ScanResult(competitors=[], target_metro_normalized='',
                          coverage=coverage, ok=False)

    if isinstance(raw_result, dict) and {'raw', 'parsed', 'parsing_error'} <= set(raw_result):
        record_ai_token_usage(
            organization=organization,
            feature=AITokenUsage.FEATURE_MARKET_COMPETITION,
            model_name=model_name,
            usage=raw_result.get('raw'),
            metadata={'event_id': str(event.id), 'stage': 'extract'},
        )
        if raw_result.get('parsing_error'):
            logger.warning(
                "Market competition extraction returned unparseable output for event %s",
                event.id,
            )
            return ScanResult(competitors=[], target_metro_normalized='',
                              coverage=coverage, ok=False)
        parsed = raw_result.get('parsed')
    else:
        parsed = raw_result

    if not isinstance(parsed, ExtractedEvents):
        if parsed is None:
            return ScanResult(competitors=[], target_metro_normalized='',
                              coverage=coverage, ok=False)
        parsed = ExtractedEvents.model_validate(parsed)

    competitors = [
        CompetitorEvent(
            name=e.name,
            date=_parse_date(e.date),
            venue_name=e.venue_name or '',
            metro_normalized=_norm(e.metro_normalized),
            genre=e.genre or '',
            platform=e.platform or '',
            source_url=e.source_url or '',
        )
        for e in parsed.events
        if (e.name or '').strip()
    ]

    competitors = [c for c in competitors if not _is_target(event, c)]  # DO5-a
    competitors = _dedupe(competitors)  # DO5-c

    return ScanResult(
        competitors=competitors,
        target_metro_normalized=_norm(parsed.target_metro_normalized),
        coverage=coverage,
        ok=True,
    )
