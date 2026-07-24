"""Low-level APNs sender.

Hand-rolled on ``httpx`` (HTTP/2) + ``PyJWT`` (ES256) rather than a heavyweight
APNs SDK — same spirit as the hand-rolled webhook HMAC signer. Auth uses an
Apple provider JWT built from the ``.p8`` Auth Key; Apple lets a provider token
be reused for 20–60 minutes, so we cache it for ~50 minutes.

``send(token, payload)`` returns a :class:`PushResult`. Callers should:
  - delete the stored token when ``result.stale`` is True (BadDeviceToken /
    Unregistered — the token will never deliver again), and
  - retry when ``result.transient`` is True (network error, timeout, 429, 5xx).
"""

import json
import logging
import time
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger(__name__)

# APNs reasons that mean the token is permanently dead (drop it from the DB).
_STALE_REASONS = frozenset({
    'BadDeviceToken',
    'Unregistered',
    'DeviceTokenNotForTopic',
})

# Reuse the provider JWT for this long. Apple requires refresh within 60 min and
# forbids minting a fresh one more than once every 20 min; 50 min sits safely
# inside that window.
_TOKEN_TTL_SECONDS = 50 * 60

# Cached (jwt_string, minted_at) — module-level, refreshed lazily.
_cached_token = None
_cached_token_at = 0.0


@dataclass
class PushResult:
    """Outcome of a single APNs send."""
    ok: bool
    status: int | None      # HTTP status, or None on a transport-level failure
    reason: str = ''        # APNs 'reason' string on failure
    skipped: bool = False   # True when credentials are unconfigured (no-op)

    @property
    def stale(self):
        """Token is permanently dead — the caller should delete it."""
        return self.status == 410 or (self.status == 400 and self.reason in _STALE_REASONS)

    @property
    def transient(self):
        """Temporary failure — the caller should retry."""
        if self.ok or self.skipped or self.stale:
            return False
        return self.status is None or self.status == 429 or (self.status or 0) >= 500


def _load_auth_key():
    """Return the .p8 PEM contents from APNS_AUTH_KEY or APNS_KEY_PATH, or None."""
    if settings.APNS_AUTH_KEY:
        return settings.APNS_AUTH_KEY
    if settings.APNS_KEY_PATH:
        try:
            with open(settings.APNS_KEY_PATH, 'r') as fh:
                return fh.read()
        except OSError:
            logger.exception("APNs: could not read APNS_KEY_PATH=%s", settings.APNS_KEY_PATH)
    return None


def _is_configured():
    return bool(settings.APNS_KEY_ID and settings.APNS_TEAM_ID and settings.APNS_BUNDLE_ID
                and (settings.APNS_AUTH_KEY or settings.APNS_KEY_PATH))


def _provider_token():
    """Build (and cache) the ES256 provider JWT used as the APNs bearer token."""
    global _cached_token, _cached_token_at
    now = time.time()
    if _cached_token and (now - _cached_token_at) < _TOKEN_TTL_SECONDS:
        return _cached_token

    import jwt  # PyJWT

    auth_key = _load_auth_key()
    if auth_key is None:
        return None
    token = jwt.encode(
        {'iss': settings.APNS_TEAM_ID, 'iat': int(now)},
        auth_key,
        algorithm='ES256',
        headers={'kid': settings.APNS_KEY_ID},
    )
    _cached_token = token
    _cached_token_at = now
    return token


def _host():
    return 'api.sandbox.push.apple.com' if settings.APNS_USE_SANDBOX else 'api.push.apple.com'


def send(token, payload):
    """Send one APNs alert. Returns a :class:`PushResult` (never raises)."""
    if not _is_configured():
        logger.warning("APNs not configured — skipping push (token=%s…)", token[:12])
        return PushResult(ok=False, status=None, skipped=True)

    provider_jwt = _provider_token()
    if provider_jwt is None:
        return PushResult(ok=False, status=None, skipped=True)

    import httpx

    url = f'https://{_host()}/3/device/{token}'
    headers = {
        'authorization': f'bearer {provider_jwt}',
        'apns-topic': settings.APNS_BUNDLE_ID,
        'apns-push-type': 'alert',
    }
    try:
        with httpx.Client(http2=True, timeout=settings.PUSH_NOTIFICATION_DELIVERY_TIMEOUT) as client:
            resp = client.post(url, headers=headers, content=json.dumps(payload))
    except httpx.HTTPError as exc:
        logger.warning("APNs transport error for token=%s…: %s", token[:12], exc)
        return PushResult(ok=False, status=None, reason=str(exc)[:200])

    if resp.status_code == 200:
        return PushResult(ok=True, status=200)

    reason = ''
    try:
        reason = (resp.json() or {}).get('reason', '')
    except (ValueError, json.JSONDecodeError):
        pass
    logger.warning("APNs send failed status=%s reason=%s token=%s…",
                   resp.status_code, reason, token[:12])
    return PushResult(ok=False, status=resp.status_code, reason=reason)
