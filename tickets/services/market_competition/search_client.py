"""Web-search client for the Market Competition Agent (Phase 2).

The one piece of real network I/O in the pipeline. Modeled on ``weather.py``'s
external-fetch discipline: a short timeout, a versioned on-disk cache, a short
failure sentinel so a flaky provider isn't hammered on every retry, and a
fail-silently-and-log policy so a search outage degrades to an empty result
(which upstream turns into ``unavailable`` — never a silent 0, D2) rather than
raising.

POC provider is Tavily (``POST https://api.tavily.com/search``, key in the body)
over ``requests`` — no new pip dependency. The provider is selected by
``settings.MARKET_COMPETITION_SEARCH_PROVIDER`` so the single source is
swappable when a structured events API lands (D1/D5).

``search_queries`` runs a batch of queries and reports *coverage* — the fraction
that returned usable results — which the scorer uses to tell a trustworthy 0
("you're clear") from a near-empty search that only looks clear (DO2).

See ``docs/technical-design/market-competition-agent.md`` §4.2, §4.3.
"""
import hashlib
import logging

import requests
from django.conf import settings
from django.core.cache import cache as django_cache

logger = logging.getLogger(__name__)

TAVILY_SEARCH_URL = "https://api.tavily.com/search"

HTTP_TIMEOUT_SECONDS = 10

# Identify ourselves to the provider like weather.py does.
USER_AGENT = "cue-events (+https://cueup.co) market-competition scan"
DEFAULT_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

SEARCH_TTL_SECONDS = 60 * 60 * 6   # 6 hours — the events landscape is slow-moving
SEARCH_FAILURE_TTL_SECONDS = 60 * 30  # 30 min — back off a failing provider

# Bumped when the cached result shape or provider routing changes so stale `[]`
# failure entries from a previous deploy don't keep suppressing results.
_SEARCH_CACHE_VERSION = 1


def _cache_key(parts):
    digest = hashlib.sha256(parts.encode("utf-8")).hexdigest()[:16]
    return f"market_comp:search:v{_SEARCH_CACHE_VERSION}:{digest}"


def _cache_key_parts(query, max_results, include_domains, exclude_domains):
    inc = ','.join(include_domains or [])
    exc = ','.join(exclude_domains or [])
    return f"{query}|{max_results}|{inc}|{exc}"


def _domains_from_setting(name):
    """Parse a comma-separated domain setting into a list (or ``[]``)."""
    raw = getattr(settings, name, '') or ''
    return [d.strip() for d in raw.split(',') if d.strip()]


def web_search(query, *, max_results=None, include_domains=None,
               exclude_domains=None):
    """Return a list of normalized result dicts for ``query``, or ``[]``.

    Each result is ``{"title", "url", "content"}``. Returns ``[]`` (never
    raises) when the API key is unset, the provider is unknown, or the request
    times out / errors / returns unparseable JSON — the warning is logged and
    the empty list flows upstream to an ``unavailable`` scan (D2).

    ``include_domains`` / ``exclude_domains`` scope the search to (or away from)
    specific sites — e.g. constrain a platform query to ``["eventbrite.com"]``,
    or drop mega-promoter/resale domains — and are passed straight through to
    the provider. When left ``None`` they fall back to the
    ``MARKET_COMPETITION_INCLUDE_DOMAINS`` / ``..._EXCLUDE_DOMAINS`` settings (the
    search-layer proxy for the deferred D4-D size tier); pass an explicit list to
    override, or ``[]`` to force open-web for that one call. They are part of the
    cache key, so a scoped query caches separately from the unscoped one.

    Caching mirrors ``weather.py``: a success caches for ``SEARCH_TTL_SECONDS``;
    a provider failure caches an empty sentinel for the shorter
    ``SEARCH_FAILURE_TTL_SECONDS`` so a retry storm doesn't hammer a flaky
    provider. The missing-key case is intentionally *not* cached, so dropping in
    ``TAVILY_API_KEY`` takes effect immediately without a cache-version bump.
    """
    if max_results is None:
        max_results = getattr(settings, 'MARKET_COMPETITION_MAX_RESULTS', 25)
    if include_domains is None:
        include_domains = _domains_from_setting('MARKET_COMPETITION_INCLUDE_DOMAINS')
    if exclude_domains is None:
        exclude_domains = _domains_from_setting('MARKET_COMPETITION_EXCLUDE_DOMAINS')

    api_key = getattr(settings, 'TAVILY_API_KEY', '')
    if not api_key:
        logger.warning(
            'Market competition search skipped: TAVILY_API_KEY is not set '
            '(query=%r)', query,
        )
        return []

    cache_key = _cache_key(
        _cache_key_parts(query, max_results, include_domains, exclude_domains)
    )
    cached = django_cache.get(cache_key)
    if cached is not None:
        return cached or []  # [] sentinel means a recent provider failure

    provider = getattr(settings, 'MARKET_COMPETITION_SEARCH_PROVIDER', 'tavily')
    if provider == 'tavily':
        results = _tavily_search(
            query, api_key, max_results, include_domains, exclude_domains
        )
    else:
        logger.warning('Unknown market competition search provider %r', provider)
        return []

    if results is None:  # provider error — cache the short failure sentinel
        django_cache.set(cache_key, [], SEARCH_FAILURE_TTL_SECONDS)
        return []

    django_cache.set(cache_key, results, SEARCH_TTL_SECONDS)
    return results


def _tavily_search(query, api_key, max_results, include_domains=None,
                   exclude_domains=None):
    """Call Tavily for one query. Returns a list of results, or ``None`` on error.

    ``None`` (distinct from ``[]``) signals the caller to cache a failure
    sentinel; a genuine empty result list is cached as a normal success.
    """
    body = {
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        # 'advanced' semantic ranking — 'basic' keyword-matches badly on
        # queries like "hip-hop events ..." (returns "hip" anatomy pages).
        "search_depth": getattr(
            settings, 'MARKET_COMPETITION_SEARCH_DEPTH', 'advanced'
        ),
    }
    if include_domains:
        body["include_domains"] = list(include_domains)
    if exclude_domains:
        body["exclude_domains"] = list(exclude_domains)

    try:
        resp = requests.post(
            TAVILY_SEARCH_URL,
            json=body,
            headers=DEFAULT_HEADERS,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning('Tavily search failed for %r: %s', query, exc)
        return None

    results = data.get("results") or []
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
        }
        for r in results
    ]


def search_queries(queries):
    """Run ``queries`` through ``web_search`` and report coverage.

    Returns ``(results_by_query, coverage)`` where ``results_by_query`` maps each
    query to its (possibly empty) result list and ``coverage`` is
    ``{"queries_total", "queries_with_results", "ratio"}``. ``ratio`` is the
    fraction of queries that returned at least one usable result — the honest
    denominator the scorer uses to flag a low-coverage 0 as ``inconclusive``
    rather than a trustworthy "you're clear" (DO2). An empty query list reports
    ``ratio == 1.0`` to match ``score_competition``'s default and avoid a
    division by zero.
    """
    results_by_query = {}
    with_results = 0
    for query in queries:
        results = web_search(query)
        results_by_query[query] = results
        if results:
            with_results += 1

    total = len(queries)
    ratio = (with_results / total) if total else 1.0
    coverage = {
        "queries_total": total,
        "queries_with_results": with_results,
        "ratio": ratio,
    }
    return results_by_query, coverage
