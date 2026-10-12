"""Apify client for the posh.vip events source (P5+, D1=B second source).

Thin `requests` wrapper over the hypebridge/posh-vip actor's
``run-sync-get-dataset-items`` endpoint, following the same external-fetch
discipline as ``search_client``/``weather.py``: a timeout, a versioned cache (a
posh scrape is slow *and* costs credits, so we cache hard), a short failure
sentinel, and a fail-silently-and-log policy — a missing token or a provider
error returns ``None`` (the source maps that to ``unavailable``, never a silent 0).

The actor's `explore?location=<city>&when=<window>` feed is location-scoped, so
the whole feed is the target metro — no per-event metro normalization needed.

See ``docs/technical-design/market-competition-agent.md`` §4.2 (sources, D1=B).
"""
import hashlib
import logging
from urllib.parse import quote

import requests
from django.conf import settings
from django.core.cache import cache as django_cache
from django.utils import timezone

logger = logging.getLogger(__name__)

RUN_SYNC_URL = (
    "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
)

USER_AGENT = "cue-events (+https://cueup.co) market-competition posh source"
DEFAULT_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

# Default success-cache TTL (24h): a city's nightlife feed for a month barely
# changes day-to-day, and every cache hit is a scan that costs $0 instead of
# ~$0.80. Overridable via MARKET_COMPETITION_POSH_CACHE_TTL. Shared across all
# events + genre/domain choices for the same (city, window) — the fetch key
# excludes the event and the genre, so re-scoring is free within the window.
DEFAULT_RESULT_TTL_SECONDS = 60 * 60 * 24
FAILURE_TTL_SECONDS = 60 * 30         # 30 min — back off a failing/blocked actor

_CACHE_VERSION = 1


def _cache_key(actor, location, when, max_events):
    parts = f"v{_CACHE_VERSION}|{actor}|{location}|{when}|{max_events}"
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


def _db_get(cache_key, ttl):
    """Return a cached feed payload if a DB entry exists and is within ``ttl``."""
    from tickets.models import MarketCompetitionFeedCache

    row = MarketCompetitionFeedCache.objects.filter(cache_key=cache_key).first()
    if row is None:
        return None
    if (timezone.now() - row.updated_at).total_seconds() >= ttl:
        return None  # stale — force a re-fetch
    return row.payload


def _db_set(cache_key, location, when, payload):
    """Upsert the fetched feed (durable across reloads/evictions, shared org-wide)."""
    from tickets.models import MarketCompetitionFeedCache

    MarketCompetitionFeedCache.objects.update_or_create(
        cache_key=cache_key,
        defaults={"location": location, "window": when, "payload": payload},
    )


def fetch_posh_events(location, when, *, max_events=None):
    """Return a list of posh.vip event dicts for ``location`` / ``when``, or ``None``.

    ``None`` (distinct from ``[]``) means the fetch could not run — no
    ``APIFY_TOKEN``, provider error, timeout, or unparseable JSON — so the caller
    reports ``unavailable`` (D2). ``[]`` is a genuine empty feed. Successful and
    failed fetches are both cached so a slow/costly actor isn't re-run on retries.
    """
    token = getattr(settings, "APIFY_TOKEN", "")
    if not token:
        logger.warning("posh.vip source: APIFY_TOKEN is not set — skipping")
        return None

    actor = getattr(settings, "MARKET_COMPETITION_POSH_ACTOR", "hypebridge~posh-vip")
    if max_events is None:
        max_events = getattr(settings, "MARKET_COMPETITION_POSH_MAX_EVENTS", 500)

    key = _cache_key(actor, location, when, max_events)
    ttl = getattr(settings, "MARKET_COMPETITION_POSH_CACHE_TTL", DEFAULT_RESULT_TTL_SECONDS)
    fail_key = f"market_comp:posh:fail:{key}"

    # Durable success cache (DB) — survives dev autoreloads + Redis evictions, and
    # is shared across every org/event/genre for this (city, window). This is the
    # layer that prevents a repeat ~$2 Apify charge.
    cached = _db_get(key, ttl)
    if cached is not None:
        logger.info(
            "posh.vip source (db cache hit) location=%r when=%r -> %d event(s)",
            location, when, len(cached),
        )
        return cached
    # Short-lived failure sentinel (cache) — just a backoff so a broken/out-of-credit
    # actor isn't hammered; cheap to lose on reload (a failed run doesn't scrape).
    if django_cache.get(fail_key) == "FAIL":
        logger.info("posh.vip source (recent failure, backing off) location=%r", location)
        return None

    explore = (
        f"https://posh.vip/explore?location={quote(location)}&when={quote(when)}"
    )
    body = {
        "startUrls": [{"url": explore}],
        "maxEvents": max_events,
        "scrapeEventDetails": True,
    }
    timeout = getattr(settings, "MARKET_COMPETITION_POSH_TIMEOUT", 120)

    try:
        resp = requests.post(
            RUN_SYNC_URL.format(actor=actor),
            # `maxItems` is required by the pay-per-result run-sync endpoint — it
            # caps both the results returned and the max charge Apify reserves
            # upfront (maxItems x price-per-result). Mirror the actor's maxEvents.
            params={"token": token, "maxItems": max_events},
            json=body,
            headers=DEFAULT_HEADERS,
            timeout=timeout,
        )
        resp.raise_for_status()
        items = resp.json()
    except requests.HTTPError as exc:
        # Apify returns an actionable JSON error (bad input, out-of-credit, …);
        # log its body so the cause is visible, not just the status code.
        body_text = ''
        try:
            body_text = exc.response.text[:500] if exc.response is not None else ''
        except Exception:
            pass
        logger.warning(
            "posh.vip source fetch failed (location=%r): %s | %s",
            location, exc, body_text,
        )
        django_cache.set(fail_key, "FAIL", FAILURE_TTL_SECONDS)
        return None
    except (requests.RequestException, ValueError) as exc:
        logger.warning("posh.vip source fetch failed (location=%r): %s", location, exc)
        django_cache.set(fail_key, "FAIL", FAILURE_TTL_SECONDS)
        return None

    if not isinstance(items, list):
        logger.warning("posh.vip source: unexpected payload type %s", type(items))
        django_cache.set(fail_key, "FAIL", FAILURE_TTL_SECONDS)
        return None

    logger.info(
        "posh.vip source location=%r when=%r -> %d event(s) (db cached %ds)",
        location, when, len(items), ttl,
    )
    _db_set(key, location, when, items)
    return items
