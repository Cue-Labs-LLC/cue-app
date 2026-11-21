"""Meta transport for the Instagram DM support agent (Phase 7).

Uses the **Instagram API with Instagram Login** (``graph.instagram.com``), not Facebook
Login — so there is no Facebook Page in the loop, the credentials are the Instagram App
ID/Secret (``settings.INSTAGRAM_APP_*``), and the account authenticates directly as an
Instagram professional account. The Send API is **POST**, so this is a purpose-built
``requests`` client (not the GET-only ``MetaAdsClient``). Non-2xx responses are folded
into a diagnosable ``InstagramGraphAPIError`` and logged.

OAuth (Business Login for Instagram):
  1. dialog → ``https://www.instagram.com/oauth/authorize`` (Instagram App ID + scopes
     ``instagram_business_basic,instagram_business_manage_insights``, plus
     ``instagram_business_manage_messages`` while the DM agent is enabled)
  2. ``exchange_instagram_code_for_token`` → short-lived token + the IG-scoped ``user_id``
  3. ``exchange_for_long_lived_instagram_token`` → 60-day token

``GraphAPISender`` is the ``InstagramSender`` backend ``get_sender`` returns under
``INSTAGRAM_SENDER_BACKEND='graph'`` when the org has credentials. Auto-answers fire in
response to an inbound DM (inside Meta's 24h window → ``messaging_type='RESPONSE'``);
human inbox replies may be later, so they carry the ``HUMAN_AGENT`` tag (7-day window). A
send still outside the window is surfaced as ``SendResult(ok=False, ...)``, never silently.
"""

import logging

import requests
from django.conf import settings

from .sender import InstagramSender, SendResult

logger = logging.getLogger(__name__)

_TIMEOUT = 20
# "Message sent outside of allowed window" — the 24h/7d messaging-window rejection.
OUT_OF_WINDOW_CODE = 10

# Business Login for Instagram endpoints (host-fixed; only the Graph host is versioned).
AUTHORIZE_URL = 'https://www.instagram.com/oauth/authorize'
_SHORT_LIVED_TOKEN_URL = 'https://api.instagram.com/oauth/access_token'
_LONG_LIVED_TOKEN_URL = 'https://graph.instagram.com/access_token'

# Scopes for the Instagram Login use case (legacy Facebook-Login scopes retired
# 2025-01-27). instagram_business_manage_insights unlocks per-post organic metrics
# (reach, views, saves, shares, follows) for the content analytics page;
# instagram_business_manage_messages (DM read/send) is only requested while the DM
# agent is enabled (settings.INSTAGRAM_DM_AGENT_ENABLED). Accounts connected before
# the insights scope was added must reconnect to grant it.
BASE_OAUTH_SCOPES = ('instagram_business_basic', 'instagram_business_manage_insights')
MESSAGING_OAUTH_SCOPE = 'instagram_business_manage_messages'


def oauth_scopes(include_messaging: bool) -> str:
    """Comma-joined OAuth scopes for the connect dialog."""
    scopes = list(BASE_OAUTH_SCOPES)
    if include_messaging:
        scopes.insert(1, MESSAGING_OAUTH_SCOPE)
    return ','.join(scopes)

# Meta rate-limit codes (app / user / BUC throttling).
RATE_LIMIT_CODES = {4, 17, 32, 613}
# "Application does not have permission" / "Permissions error" — the token lacks a scope.
PERMISSION_CODES = {10, 200}
# OAuth token invalid / expired.
TOKEN_ERROR_CODE = 190


MEDIA_FIELDS = (
    "id,caption,media_type,media_product_type,media_url,thumbnail_url,"
    "permalink,timestamp,like_count,comments_count"
)


def _parse_insight_rows(rows) -> dict:
    """``{metric_name: value}`` from an insights ``data`` array."""
    values = {}
    for row in rows or []:
        name = row.get("name")
        points = row.get("values") or []
        if points:
            value = points[0].get("value")
        else:
            value = (row.get("total_value") or {}).get("value")
        if name and isinstance(value, (int, float)):
            values[name] = value
    return values


class InstagramGraphAPIError(Exception):
    """A non-2xx response from the Instagram API, with structured fields."""

    def __init__(self, message, *, code=None, subcode=None, fbtrace_id=None,
                 status=None, payload=None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.fbtrace_id = fbtrace_id
        self.status = status
        self.payload = payload

    @property
    def is_out_of_window(self) -> bool:
        # Code 10 is overloaded (also the "Human Agent not approved" error), so require the
        # message to actually be about the messaging window to distinguish them.
        return self.code == OUT_OF_WINDOW_CODE and 'window' in str(self).lower()

    @property
    def is_rate_limited(self) -> bool:
        return self.code in RATE_LIMIT_CODES

    @property
    def is_permission_error(self) -> bool:
        # Code 10 is overloaded; exclude the messaging-window and story "not enough
        # viewers" flavours so only a genuinely missing scope reads as a permission gap.
        if self.code not in PERMISSION_CODES:
            return False
        text = str(self).lower()
        return 'window' not in text and 'not enough viewers' not in text

    @property
    def is_token_error(self) -> bool:
        return self.code == TOKEN_ERROR_CODE


def _graph_base() -> str:
    return f"https://graph.instagram.com/{settings.INSTAGRAM_GRAPH_API_VERSION}"


def exchange_instagram_code_for_token(code: str, redirect_uri: str) -> dict:
    """Exchange an auth code for a short-lived IG user token.

    Returns ``{access_token, user_id, permissions}`` — ``user_id`` is the Instagram-scoped
    account id that also appears in webhook ``entry[].id`` (the org-resolution routing key).
    """
    try:
        response = requests.post(_SHORT_LIVED_TOKEN_URL, data={
            'client_id': settings.INSTAGRAM_APP_ID,
            'client_secret': settings.INSTAGRAM_APP_SECRET,
            'grant_type': 'authorization_code',
            'redirect_uri': redirect_uri,
            'code': code,
        }, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise InstagramGraphAPIError(f"Instagram OAuth request failed: {exc}") from exc
    if not 200 <= response.status_code < 300:
        raise _error_from_response(response)
    return response.json()


def exchange_for_long_lived_instagram_token(short_token: str) -> dict:
    """Exchange a short-lived token for a 60-day one (GET, per docs). Returns
    ``{access_token, expires_in}``. Callers treat failure as non-fatal and fall back to
    the short-lived token (see the connect callback)."""
    try:
        response = requests.get(_LONG_LIVED_TOKEN_URL, params={
            'grant_type': 'ig_exchange_token',
            'client_secret': settings.INSTAGRAM_APP_SECRET,
            'access_token': short_token,
        }, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise InstagramGraphAPIError(f"Instagram OAuth request failed: {exc}") from exc
    if not 200 <= response.status_code < 300:
        raise _error_from_response(response)
    return response.json()


class InstagramGraphClient:
    """Thin ``graph.instagram.com`` client for the IG endpoints Phase 7 needs."""

    def __init__(self, access_token: str):
        self.access_token = access_token
        self.base_url = _graph_base()

    def get_me(self) -> dict:
        """The connected account: ``{user_id, username}`` (user_id = webhook routing key)."""
        return self._get("/me", {"fields": "user_id,username"})

    def subscribe_to_messages(self) -> dict:
        """Subscribe this app to the account's ``messages`` webhook field."""
        return self._post("/me/subscribed_apps", {"subscribed_fields": "messages"})

    def send_message(self, ig_id: str, recipient_id: str, text: str, *,
                     messaging_type: str = "RESPONSE", tag: str | None = None) -> dict:
        """POST one text DM. Returns the response (carries ``message_id``)."""
        body = {
            "recipient": {"id": recipient_id},
            "message": {"text": text},
            "messaging_type": messaging_type,
        }
        if tag:
            body["tag"] = tag
        return self._post(f"/{ig_id}/messages", json_body=body)

    # -- organic content insights -------------------------------------------

    def get_account_profile(self) -> dict:
        """Public profile counters for the connected account."""
        return self._get("/me", {
            "fields": "user_id,username,followers_count,media_count,profile_picture_url",
        })

    def list_media(self, *, since=None, until=None, max_items: int = 100,
                   page_size: int = 50) -> list:
        """Feed posts + reels, newest first (stories are a separate edge).

        ``since`` / ``until`` are aware datetimes; Meta filters server-side and paging
        stops at ``max_items``. Callers still filter by timestamp defensively.
        """
        return self._paged_media(MEDIA_FIELDS, since, until, max_items, page_size)

    def list_media_with_insights(self, *, metrics, since=None, until=None,
                                 max_items: int = 1000, page_size: int = 50) -> list:
        """Posts with their lifetime insights inlined via field expansion.

        One request per page instead of one insights call per post. Given the union of
        format-specific metrics, Meta returns only the ones each post's type supports
        (follows/profile_visits on feed posts, watch time on reels), so callers can ask
        for everything at once. Each item's ``insights`` is replaced by a
        ``{metric: value}`` dict, or ``None`` when Meta returned no insights for it.
        """
        fields = f"{MEDIA_FIELDS},insights.metric({','.join(metrics)})"
        items = self._paged_media(fields, since, until, max_items, page_size)
        for item in items:
            rows = (item.get("insights") or {}).get("data")
            item["insights"] = _parse_insight_rows(rows) if rows else None
        return items

    def get_media_with_insights(self, media_id: str, metrics) -> dict:
        """One post (any age) with its insights inlined, shaped like
        ``list_media_with_insights`` items."""
        item = self._get(f"/{media_id}", {
            "fields": f"{MEDIA_FIELDS},insights.metric({','.join(metrics)})",
        })
        rows = (item.get("insights") or {}).get("data")
        item["insights"] = _parse_insight_rows(rows) if rows else None
        return item

    def get_media_children(self, media_id: str) -> list:
        """A carousel's slides: ``[{id, media_type, media_url, thumbnail_url}]``."""
        payload = self._get(f"/{media_id}", {
            "fields": "children{media_type,media_url,thumbnail_url}",
        })
        return (payload.get("children") or {}).get("data") or []

    def get_media_insights(self, media_id: str, metrics) -> dict:
        """Lifetime insights for one post as ``{metric_name: value}``."""
        payload = self._get(f"/{media_id}/insights", {"metric": ",".join(metrics)})
        return _parse_insight_rows(payload.get("data") or [])

    def _paged_media(self, fields, since, until, max_items, page_size) -> list:
        params = {"fields": fields, "limit": page_size}
        if since is not None:
            params["since"] = int(since.timestamp())
        if until is not None:
            params["until"] = int(until.timestamp())
        payload = self._get("/me/media", params)
        items = []
        while True:
            items.extend(payload.get("data") or [])
            next_url = (payload.get("paging") or {}).get("next")
            if len(items) >= max_items or not next_url:
                break
            payload = self._get_url(next_url)
        return items[:max_items]

    # -- transport -----------------------------------------------------------

    def _get_url(self, url: str) -> dict:
        """GET a fully-formed paging URL (Meta embeds the access token in it)."""
        try:
            response = requests.get(url, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            raise InstagramGraphAPIError(f"Instagram Graph request failed: {exc}") from exc
        if not 200 <= response.status_code < 300:
            raise _error_from_response(response)
        return response.json()

    def _get(self, path: str, params: dict) -> dict:
        request_params = {"access_token": self.access_token, **params}
        try:
            response = requests.get(f"{self.base_url}{path}", params=request_params, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            raise InstagramGraphAPIError(f"Instagram Graph request failed: {exc}") from exc
        if not 200 <= response.status_code < 300:
            raise _error_from_response(response)
        return response.json()

    def _post(self, path: str, data: dict | None = None, *, json_body: dict | None = None) -> dict:
        # Send POSTs are NOT retried: POST isn't idempotent and the orchestration task /
        # inbox dedupe on provider_message_id already handle retries.
        try:
            response = requests.post(
                f"{self.base_url}{path}",
                params={"access_token": self.access_token},
                data=data,
                json=json_body,
                timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise InstagramGraphAPIError(f"Instagram Graph request failed: {exc}") from exc
        if not 200 <= response.status_code < 300:
            raise _error_from_response(response)
        return response.json()


class GraphAPISender(InstagramSender):
    """Real Instagram sender, bound to one organization's credentials."""

    def __init__(self, organization):
        self.organization = organization
        self.ig_id = organization.instagram_business_account_id
        self.client = InstagramGraphClient(organization.instagram_page_access_token)

    def send_text(self, recipient_id: str, text: str, *, human_agent: bool = False) -> SendResult:
        # Always try a normal in-window RESPONSE first — it needs no special permission and
        # covers auto-answers (always in-window) and the common human reply to a recent DM.
        try:
            return self._send(recipient_id, text, messaging_type="RESPONSE")
        except InstagramGraphAPIError as exc:
            # Only a human reply that Meta rejects as OUTSIDE the 24h window falls back to the
            # HUMAN_AGENT tag (7-day window) — which requires the approved Human Agent feature.
            if not (human_agent and exc.is_out_of_window):
                return self._fail(recipient_id, "RESPONSE", exc)
        try:
            return self._send(recipient_id, text, messaging_type="MESSAGE_TAG", tag="HUMAN_AGENT")
        except InstagramGraphAPIError as exc:
            return self._fail(recipient_id, "MESSAGE_TAG", exc)

    def _send(self, recipient_id: str, text: str, *, messaging_type: str, tag=None) -> SendResult:
        payload = self.client.send_message(
            self.ig_id, recipient_id, text, messaging_type=messaging_type, tag=tag,
        )
        return SendResult(ok=True, provider_message_id=str(payload.get("message_id", "") or ""))

    def _fail(self, recipient_id: str, messaging_type: str, exc) -> SendResult:
        logger.warning(
            "Instagram send failed (org=%s recipient=%s type=%s): %s",
            getattr(self.organization, 'id', '?'), recipient_id, messaging_type, exc,
        )
        return SendResult(ok=False, error=str(exc))


def _error_from_response(response) -> InstagramGraphAPIError:
    """Build a diagnosable error from a non-2xx Instagram response.

    Handles both the Graph shape (``{"error": {code, error_subcode, ...}}``) and the
    ``api.instagram.com`` OAuth shape (``{"error_type", "code", "error_message"}``).
    """
    status = response.status_code
    try:
        payload = response.json()
    except ValueError:
        payload = None

    data = payload or {}
    error = data.get("error") or {}
    code = error.get("code", data.get("code"))
    subcode = error.get("error_subcode")
    fbtrace_id = error.get("fbtrace_id")
    detail = (
        error.get("error_user_msg")
        or error.get("message")
        or data.get("error_message")
        or f"Instagram API error ({status})."
    )
    code_bits = []
    if code is not None:
        code_bits.append(f"code {code}")
    if subcode:
        code_bits.append(f"subcode {subcode}")
    prefix = (
        f"Instagram API error ({', '.join(code_bits)})"
        if code_bits else f"Instagram API error ({status})"
    )
    # Log the endpoint (method + path, query stripped so the access_token isn't leaked)
    # so a failure points at the exact call that broke.
    request = getattr(response, 'request', None)
    method = getattr(request, 'method', '?')
    endpoint = (getattr(response, 'url', '') or '').split('?')[0]
    logger.warning(
        "Instagram API error: %s %s status=%s code=%s subcode=%s fbtrace_id=%s payload=%s",
        method, endpoint, status, code, subcode, fbtrace_id, payload,
    )
    return InstagramGraphAPIError(
        f"{prefix}: {detail}",
        code=code, subcode=subcode, fbtrace_id=fbtrace_id, status=status, payload=payload,
    )
