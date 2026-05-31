"""Per-event campaign matcher result cache.

Mailchimp/SlickText/Meta matchers each call the OpenAI API with the org's
campaign list to rank candidates for a given event. The answer is stable until
either the campaign list changes (new sync) or a user links/unlinks a campaign,
so we cache the structured result keyed by source + event + a fingerprint of
the candidate id set, plus a per-event version that callers bump to invalidate.
"""

import hashlib
import json

from django.conf import settings
from django.core.cache import cache as django_cache


def _fingerprint(candidate_ids):
    sorted_ids = sorted(str(cid) for cid in candidate_ids if cid is not None)
    payload = json.dumps(sorted_ids, separators=(",", ":"))
    return hashlib.md5(payload.encode()).hexdigest()[:12]


def _version(source, event_id):
    return django_cache.get(f"campaign_match_ver:{source}:{event_id}", 0)


def _key(source, event_id, candidate_ids):
    version = _version(source, event_id)
    return f"campaign_match:{source}:v{version}:{event_id}:{_fingerprint(candidate_ids)}"


def get(source, event_id, candidate_ids):
    return django_cache.get(_key(source, event_id, candidate_ids))


def set(source, event_id, candidate_ids, value):
    ttl = getattr(settings, "CAMPAIGN_MATCH_CACHE_TTL", 3600)
    django_cache.set(_key(source, event_id, candidate_ids), value, ttl)


def invalidate(source, event_id):
    """Bump per-event version so the next match re-runs."""
    version_key = f"campaign_match_ver:{source}:{event_id}"
    try:
        django_cache.incr(version_key)
    except ValueError:
        django_cache.set(version_key, 1, timeout=None)
