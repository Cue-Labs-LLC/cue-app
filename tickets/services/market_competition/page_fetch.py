"""Buy-page fetcher for genre detection (Phase 5, D1=A).

For an external event the organizer gives a ``ticket_link`` pointing at the
listing on another platform (Eventbrite, DICE, a venue box office, …). That page
usually names the genre far more reliably than the bare event title, so we fetch
it and feed its visible text into ``derive_genre_hints``.

Follows the same external-fetch discipline as ``search_client``/``weather.py``: a
short timeout, a versioned cache, a short failure sentinel, and a
fail-silently-and-log policy — a dead or slow link degrades to an empty string
(auto-detection falls back to the event's own text), never an exception.

**SSRF**: the URL is organizer-supplied, so it is validated with the webhook SSRF
guard (``is_webhook_url_allowed`` — https-only outside DEBUG, private/loopback/
reserved hosts blocked) before any request. We only ever fetch the *external*
buy-page link; we never fetch our own Cue-hosted page (its content is just the
event's own data, already available locally).

See ``docs/technical-design/market-competition-agent.md`` §4.2 (D1=A).
"""
import hashlib
import logging
import re

import requests
from django.core.cache import cache as django_cache

from tickets.services.webhooks.validation import is_webhook_url_allowed

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 10
USER_AGENT = "cue-events (+https://cueup.co) market-competition genre probe"
DEFAULT_HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}

PAGE_TTL_SECONDS = 60 * 60 * 6        # 6h — a listing's genre is slow-moving
PAGE_FAILURE_TTL_SECONDS = 60 * 30    # 30 min — back off a failing/blocked link
MAX_TEXT_CHARS = 5000                  # plenty for keyword matching; bounds memory
MAX_BYTES = 2_000_000                  # don't slurp an unexpectedly huge response

_CACHE_VERSION = 1

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _cache_key(url):
    digest = hashlib.sha256((url or '').encode('utf-8')).hexdigest()[:16]
    return f"market_comp:page:v{_CACHE_VERSION}:{digest}"


def _html_to_text(html):
    """Collapse an HTML document to its visible text (title + body), truncated."""
    if not html:
        return ''
    text = _SCRIPT_STYLE_RE.sub(' ', html)
    text = _TAG_RE.sub(' ', text)
    # Decode the handful of entities that actually matter for keyword matching.
    for ent, ch in (('&amp;', '&'), ('&nbsp;', ' '), ('&#39;', "'"),
                    ('&quot;', '"'), ('&lt;', '<'), ('&gt;', '>')):
        text = text.replace(ent, ch)
    text = _WS_RE.sub(' ', text).strip()
    return text[:MAX_TEXT_CHARS]


def fetch_page_text(url):
    """Return the visible text of ``url`` for genre detection, or ``''``.

    Never raises: an unset/unsafe URL, a timeout, an HTTP error, or a non-HTML
    response all yield ``''`` (logged). Successful and failed fetches are both
    cached so a flaky link isn't hammered across a scan's retries.
    """
    url = (url or '').strip()
    if not url:
        return ''

    key = _cache_key(url)
    cached = django_cache.get(key)
    if cached is not None:
        return cached

    if not is_webhook_url_allowed(url):
        logger.warning("Buy-page genre probe: unsafe/blocked URL skipped: %s", url)
        django_cache.set(key, '', PAGE_FAILURE_TTL_SECONDS)
        return ''

    try:
        resp = requests.get(
            url, headers=DEFAULT_HEADERS, timeout=HTTP_TIMEOUT_SECONDS,
            stream=True,
        )
        resp.raise_for_status()
        ctype = (resp.headers.get('Content-Type') or '').lower()
        if 'html' not in ctype and 'text' not in ctype:
            logger.info("Buy-page genre probe: non-HTML (%s) at %s", ctype, url)
            django_cache.set(key, '', PAGE_FAILURE_TTL_SECONDS)
            return ''
        raw = resp.content[:MAX_BYTES]
        html = raw.decode(resp.encoding or 'utf-8', errors='replace')
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Buy-page genre probe failed for %s: %s", url, exc)
        django_cache.set(key, '', PAGE_FAILURE_TTL_SECONDS)
        return ''

    text = _html_to_text(html)
    django_cache.set(key, text, PAGE_TTL_SECONDS)
    return text
