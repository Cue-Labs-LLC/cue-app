"""Calculator for the Market Competition Agent (Phase 3).

Orchestrates one on-demand competition scan end to end:

    derive genre hints (deterministic keyword map — D1) →
    scanner.scan_competitors (fixed query plan + one LLM extraction) →
    score_competition (pure, deterministic) →
    narrative (one LLM call; failure degrades gracefully — D6)

and returns a :class:`CompetitionResult` in memory. Persistence to the Event row,
the Celery task, the in-progress lock, and the daily cap are Phase 4 — this layer
is pure compute so it can be driven synchronously from the ``scan_event_competition``
management command (the Phase 3 demo surface).

``status`` honors D2 — a missing ``TAVILY_API_KEY``, a guard failure (no venue /
city / date), an extraction failure, or an all-empty search returns
``unavailable``, never a silent ``Low (0)``.

See ``docs/technical-design/market-competition-agent.md`` §4.2 (calculator), §3,
D1/D2/D6, DO5-e/f.
"""
import logging
import re
from functools import lru_cache

from django.conf import settings

from tickets.models import AITokenUsage
from tickets.services.ai_metering import record_ai_token_usage
from tickets.services.ai_tracing import trace_config

from . import sources
from .scoring import score_competition
from .types import CompetitionResult, TargetEvent

logger = logging.getLogger(__name__)

# Deterministic genre taxonomy (D1): canonical genre -> trigger keywords matched
# against the event name + talent lineup + description. Zero LLM cost, fully
# unit-testable, and an empty result is valid — the scorer's DO5-f fallback takes
# over (city+date density, genre weight dropped). This is a lightweight POC
# stand-in; a richer taxonomy is a future refinement.
GENRE_KEYWORDS = {
    'hip-hop': ['hip-hop', 'hip hop', 'rap', 'trap'],
    'r&b': ['r&b', 'rnb', 'soul'],
    'house': ['house music', 'deep house', 'tech house'],
    'techno': ['techno'],
    'edm': ['edm', 'electronic', 'dubstep', 'bass music', 'rave'],
    'rock': ['rock', 'punk', 'grunge'],
    'metal': ['metal', 'hardcore'],
    'indie': ['indie', 'alternative'],
    'pop': ['pop'],
    'jazz': ['jazz'],
    'country': ['country', 'bluegrass', 'americana'],
    'latin': ['latin', 'reggaeton', 'salsa', 'bachata', 'cumbia'],
    'afrobeats': ['afrobeats', 'afrobeat', 'amapiano'],
    'reggae': ['reggae'],
    'dancehall': ['dancehall'],
    'comedy': ['comedy', 'stand-up', 'stand up', 'standup'],
    'drag': ['drag'],
    'dj': ['dj set', 'dj night'],
}


_GENRE_MAX = 6


def genre_options() -> list:
    """The canonical genres to offer as selectable options in the UI (D4/P5).

    The same vocabulary auto-detection draws from (``GENRE_KEYWORDS``), in a stable
    display order — so when auto-detect finds nothing the organizer can still pick
    one or more from the list instead of guessing the spelling.
    """
    return list(GENRE_KEYWORDS.keys())


def parse_genres(raw, *, max_genres=_GENRE_MAX) -> list:
    """Canonical parse of a comma-separated genre string → clean hint list.

    Trims, lower-cases, de-dupes (stable order), drops blanks, and caps the count
    (each genre adds search queries — DO5-g). Shared by the model's ``Event.genre``,
    the panel override, and ``resolve_event_genres`` so every path normalizes the
    same way.
    """
    seen = []
    for part in str(raw or '').split(','):
        g = part.strip().lower()
        if g and g not in seen:
            seen.append(g)
        if len(seen) >= max_genres:
            break
    return seen


@lru_cache(maxsize=None)
def _keyword_pattern(keyword):
    """A compiled word-boundary matcher for one genre keyword.

    Word boundaries matter: a plain substring check tags "edm" from the "rave" in
    "t​rave​l" (travel) and "rap" from "photo​grap​hy" — both
    seen in real posh descriptions. ``\\b`` only matches a genuine word edge, and a
    digit/ampersand-aware edge for keywords like "r&b" whose boundary isn't a
    standard ``\\w``/``\\W`` transition.
    """
    # Optional trailing "s" keeps legit plurals ("dj sets", "raves") while the
    # boundaries still reject mid-word collisions ("travel", "photography").
    return re.compile(r'(?<![\w&])' + re.escape(keyword) + r's?(?![\w&])')


def genres_in_text(text) -> list:
    """Canonical genres whose trigger keywords appear in ``text`` (deduped, stable).

    Pure, LLM-free word-boundary matching over ``GENRE_KEYWORDS``. Shared by
    ``derive_genre_hints`` (target event) and the posh source (per-competitor
    genre). An empty list is a valid outcome (DO5-f density fallback).
    """
    haystack = (text or '').lower()
    hints = []
    for genre, keywords in GENRE_KEYWORDS.items():
        if any(_keyword_pattern(kw).search(haystack) for kw in keywords) and genre not in hints:
            hints.append(genre)
    return hints


def derive_genre_hints(event, *, extra_text='') -> list:
    """Return the canonical genres the event's text suggests (deduped, stable order).

    Scans ``event.name`` + every ``talent_lineup`` name + ``event.description`` plus
    ``extra_text`` (the fetched buy-page content — D1=A; the caller does the I/O so
    this function stays pure/offline). An empty list is a valid, expected outcome =>
    the scorer's no-confident-genre density fallback applies (DO5-f).
    """
    parts = [event.name or '', getattr(event, 'description', '') or '', extra_text or '']
    try:
        parts.extend(t.name for t in event.talent_lineup.all())
    except Exception:
        # Unsaved event or no reverse manager available — name/description still work.
        pass
    return genres_in_text(' '.join(parts))


def event_buy_page_url(event) -> str:
    """The external buy-page URL to probe for genre, or ``''``.

    Only the organizer-supplied ``ticket_link`` (external listings) is fetched; a
    Cue-hosted direct-ticketing page is never fetched (its content is the event's
    own data, already read locally).
    """
    return (getattr(event, 'ticket_link', '') or '').strip()


def event_has_buy_page(event) -> bool:
    """True if the event has a buy page at all — an external link or a Cue page.

    Direct-ticketing events always have a Cue-hosted public buy page, so they
    always pass; external events pass only with a ``ticket_link`` (D2=A).
    """
    from tickets.models import TICKETING_TYPE_DIRECT

    if event_buy_page_url(event):
        return True
    return getattr(event, 'ticketing_type', '') == TICKETING_TYPE_DIRECT


def event_has_genre_source(event) -> bool:
    """True if a scan can determine a genre (or run density) without prompting.

    Satisfied by an explicit ``Event.genre``, a non-empty description, or a buy
    page (external link or Cue page). When none hold, the UI blocks the scan and
    prompts the organizer to set a genre first (D2=A).
    """
    if parse_genres(getattr(event, 'genre', '')):
        return True
    if (getattr(event, 'description', '') or '').strip():
        return True
    return event_has_buy_page(event)


def resolve_event_genres(event, *, extra_text='') -> list:
    """The genres a scan should use, honoring the organizer's explicit choice.

    An explicit ``Event.genre`` wins (the organizer set it — don't second-guess);
    otherwise fall back to keyword auto-detection over the event's text + the
    fetched buy page (``extra_text``). Used both offline (``extra_text=''``) for the
    panel pre-fill and comparison baseline, and with the fetched page at scan time.
    """
    explicit = parse_genres(getattr(event, 'genre', ''))
    if explicit:
        return explicit
    return derive_genre_hints(event, extra_text=extra_text)


def _unavailable(reason=''):
    """A result that renders honestly as 'couldn't scan' (D2) — never a silent 0/Low."""
    return CompetitionResult(
        score=0,
        label='Low',
        status='unavailable',
        competitors=[],
        undated=[],
        counts={},
        coverage={},
        summary='',
    )


class MarketCompetitionCalculator:
    """Standard ``__init__(self, organization)`` + ``calculate(event)`` service."""

    def __init__(self, organization, user=None):
        self.organization = organization
        self.user = user

    def calculate(self, event, genre_hints=None, *,
                  include_domains=None, exclude_domains=None) -> CompetitionResult:
        """Scan + score + narrate one event.

        ``genre_hints`` defaults to the deterministic keyword-map derivation
        (D1); pass an explicit list to override it (e.g. from the
        ``scan_event_competition --genres`` flag for ad-hoc tuning). An explicit
        empty list forces the DO5-f city+date density fallback.

        ``include_domains`` / ``exclude_domains`` scope the web searches to (or
        away from) specific hostnames for this scan — an allow-list / deny-list
        the organizer controls per scan, rather than a global setting. ``None``
        leaves the search layer's default; pass a list to override.
        """
        venue = getattr(event, 'venue', None)
        city = (getattr(venue, 'city', '') or '').strip() if venue else ''
        if not (venue and city and event.start_date):
            logger.info(
                "Market competition scan skipped for event %s: missing venue/city/date",
                getattr(event, 'id', '?'),
            )
            return _unavailable('missing venue/city/date')

        if genre_hints is None:
            # Auto path. An explicit Event.genre wins outright (skip the fetch —
            # the organizer already told us). Otherwise detect from the event's
            # text plus the fetched external buy page (D1=A). A caller-supplied
            # override list (incl. []) skips all of this.
            explicit = parse_genres(getattr(event, 'genre', ''))
            if explicit:
                genre_hints = explicit
            else:
                from .page_fetch import fetch_page_text
                url = event_buy_page_url(event)
                page_text = fetch_page_text(url) if url else ''
                genre_hints = derive_genre_hints(event, extra_text=page_text)
        else:
            genre_hints = list(genre_hints)
        scan = sources.fetch_competitors(
            self.organization, event, genre_hints,
            include_domains=include_domains, exclude_domains=exclude_domains,
        )

        coverage = scan.coverage or {}
        all_empty = (
            coverage.get('queries_total', 0) > 0
            and coverage.get('queries_with_results', 0) == 0
        )
        if not scan.ok or all_empty:
            logger.info(
                "Market competition scan unavailable for event %s (ok=%s, coverage=%s)",
                event.id, scan.ok, coverage,
            )
            return _unavailable('extraction failed or empty search')

        target = TargetEvent(
            metro_normalized=scan.target_metro_normalized,
            start_date=event.start_date,
            end_date=event.end_date,
            genre_hints=genre_hints,
        )
        window_days = getattr(settings, 'MARKET_COMPETITION_DATE_WINDOW_DAYS', 3)
        result = score_competition(
            target, scan.competitors, undated=[], coverage=coverage,
            window_days=window_days,
        )
        # Record exactly which genres this scan searched for, so the UI can show
        # the (auto-detected or overridden) genres transparently and pre-fill the
        # editable field on reload. Empty => city-wide density fallback (DO5-f).
        result.genres = list(genre_hints)

        result.summary = self._generate_narrative(event, result)
        return result

    def _generate_narrative(self, event, result) -> str:
        """One LLM call producing a short actionable read. Failure => '' (D6).

        The score/label/status are already set; a narrative failure must never
        change them — it only leaves the summary blank.
        """
        from langchain_openai import ChatOpenAI

        model_name = getattr(settings, 'OPENAI_MODEL', 'gpt-4o')
        prompt = self._build_narrative_prompt(event, result)

        try:
            llm = ChatOpenAI(
                model=model_name,
                api_key=getattr(settings, 'OPENAI_API_KEY', ''),
                temperature=0.3,
                stream_usage=True,
            )
            response = llm.invoke(
                [{'role': 'user', 'content': prompt}],
                **trace_config(
                    name='market-competition-narrative',
                    tags=['market-competition', 'narrative'],
                    metadata={'event_id': str(event.id)},
                ),
            )
        except Exception as exc:
            logger.warning(
                "Market competition narrative failed for event %s: %s", event.id, exc
            )
            return ''

        record_ai_token_usage(
            organization=self.organization,
            feature=AITokenUsage.FEATURE_MARKET_COMPETITION,
            model_name=model_name,
            user=self.user,
            usage=response,
            metadata={'event_id': str(event.id), 'stage': 'narrative'},
        )

        text = (getattr(response, 'content', '') or '').strip()
        if not text:
            logger.info(
                "Market competition narrative returned empty text for event %s", event.id
            )
        return text

    def _build_narrative_prompt(self, event, result):
        counts = result.counts or {}
        lines = []
        for c in result.competitors[:10]:
            when = c.date.isoformat() if c.date else 'date unknown'
            lines.append(f"- {c.name} ({c.genre or 'genre n/a'}) at {c.venue_name or 'venue n/a'}, {when} [{c.platform or 'platform n/a'}]")
        competitor_block = '\n'.join(lines) if lines else '(no matching competing events)'

        city = (getattr(event.venue, 'city', '') or '').strip()
        return (
            f'You are advising the organizer of "{event.name}" in {city} on how much '
            f'competition it faces from similar events in the same market around its '
            f'date. Write ONE short, direct paragraph (2-3 sentences) they can act on. '
            f'Do not restate a numeric score; describe the competitive picture and what '
            f'it implies (e.g. consider date, pricing, or promotion). Use only the data '
            f'below — do not invent events.\n\n'
            f'Competitive density: {result.label}.\n'
            f'Matching competing events in-window, same metro: {counts.get("genre_matched", 0)} '
            f'(of {counts.get("total", 0)} found; {counts.get("undated", 0)} had no confident date).\n'
            f'Competing events:\n{competitor_block}\n'
        )


def calculate_event_competition(organization, event, genre_hints=None, *,
                                include_domains=None, exclude_domains=None) -> CompetitionResult:
    """Single public entry point (TDD §3): scan + score + narrate one event.

    ``genre_hints=None`` auto-derives from the event (D1); pass an explicit list
    to override. ``include_domains`` / ``exclude_domains`` scope the web searches
    for this scan (allow-list / deny-list of hostnames).
    """
    return MarketCompetitionCalculator(organization).calculate(
        event, genre_hints=genre_hints,
        include_domains=include_domains, exclude_domains=exclude_domains,
    )
