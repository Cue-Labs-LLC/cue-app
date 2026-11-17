import html
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# Meta reports overlapping purchase action types; use the first present
# (never sum across types — that double counts the same conversions).
PURCHASE_ACTION_TYPES = (
    'omni_purchase',
    'purchase',
    'offsite_conversion.fb_pixel_purchase',
    'onsite_web_purchase',
)

# Meta error codes that are explicitly "transient — retry later":
# 1 = "An unknown error has occurred.", 2 = "Service temporarily unavailable".
_META_TRANSIENT_CODES = (1, 2)
# Throttling: app/user/page-level call limits (4/17/32), custom-level (613), and
# the Marketing API's ads-insights / ads-management throttles (800xx).
_META_RATE_LIMIT_CODES = (4, 17, 32, 613, 80000, 80003, 80004, 80014)


class MetaAdsAPIError(Exception):
    """Raised when the Meta Graph API returns an error response.

    Carries the structured Graph API error fields (code/subcode/fbtrace_id) so
    callers can branch on them and so the generic ``"An unknown error has
    occurred."`` message is no longer the only thing we know about a failure.
    """

    def __init__(self, message, *, code=None, subcode=None, fbtrace_id=None,
                 status=None, payload=None):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.fbtrace_id = fbtrace_id
        self.status = status
        self.payload = payload

    @property
    def is_transient(self) -> bool:
        return self.code in _META_TRANSIENT_CODES

    @property
    def is_rate_limited(self) -> bool:
        return self.code in _META_RATE_LIMIT_CODES


@dataclass(frozen=True)
class CampaignInsights:
    """Lifetime campaign metrics pulled from the Meta Insights API.

    purchases/purchase_value are None when Meta returned no conversion data
    (no actions reported), and 0 when actions exist but none are purchases.
    """
    spend: Decimal
    purchases: int | None
    purchase_value: Decimal | None


def _extract_purchase_count(entries) -> int | None:
    value = _first_purchase_value(entries)
    if value is None:
        return 0 if entries else None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        raise MetaAdsAPIError("Meta returned an invalid purchase count.")


def _extract_purchase_amount(entries) -> Decimal | None:
    value = _first_purchase_value(entries)
    if value is None:
        return Decimal('0.00') if entries else None
    try:
        return Decimal(str(value)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError):
        raise MetaAdsAPIError("Meta returned an invalid purchase value.")


def _first_purchase_value(entries):
    """Return the raw value for the highest-priority purchase action type, or None."""
    by_type = {row.get('action_type'): row.get('value') for row in (entries or [])}
    for action_type in PURCHASE_ACTION_TYPES:
        if action_type in by_type:
            return by_type[action_type]
    return None


# Thumbnails default to 64px; ask for 600px so they hold up in the detail panel.
AD_DETAIL_FIELDS = (
    "effective_status,preview_shareable_link,"
    "creative.thumbnail_width(600).thumbnail_height(600)"
    "{id,image_hash,thumbnail_url,image_url,title,body,call_to_action_type,object_type,"
    "object_story_spec,asset_feed_spec}"
)
# Just enough of each creative to tell which video/image it uses (see _creative_key).
AD_CREATIVE_KEY_FIELDS = (
    "creative{id,image_hash,object_story_spec{video_data{video_id},link_data{image_hash}},"
    "asset_feed_spec{videos{video_id},images{hash}}}"
)

# Facebook mobile feed is the one preview format every ad supports (Instagram
# formats fail for ads without IG placements) and it renders a playable video.
AD_PREVIEW_FORMAT = 'MOBILE_FEED_STANDARD'
_PREVIEW_IFRAME_SRC = re.compile(
    r'src="(https://(?:business|www)\.facebook\.com/ads/api/preview_iframe\.php[^"]*)"'
)

_CREATIVE_FORMATS = {
    'VIDEO': 'Video',
    'PHOTO': 'Image',
    'SHARE': 'Link',
    'STATUS': 'Post',
}


def _humanize_enum(value: str) -> str:
    """``SEE_DETAILS`` -> ``See details``."""
    return value.replace('_', ' ').capitalize() if value else ''


def _safe_url(value) -> str:
    """Only pass through http(s) URLs (never ``javascript:`` etc.) to templates."""
    value = (value or '').strip()
    return value if value.startswith(('https://', 'http://')) else ''


def _unique(values) -> list[str]:
    seen, result = set(), []
    for value in values:
        value = (value or '').strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _creative_key(creative: dict) -> str:
    """Identify the underlying video/image an ad's creative uses.

    Duplicating an ad ("- Copy") or reusing a video across ad sets creates new
    ad and creative ids but keeps the same video id / image hash, so those are
    what identify "the same creative". Falls back to the creative id.
    """
    story = creative.get('object_story_spec') or {}
    feed = creative.get('asset_feed_spec') or {}
    video_id = (
        (story.get('video_data') or {}).get('video_id')
        or next((v.get('video_id') for v in feed.get('videos') or [] if v.get('video_id')), '')
    )
    if video_id:
        return f'video:{video_id}'
    image_hash = (
        (story.get('link_data') or {}).get('image_hash')
        or creative.get('image_hash')
        or next((i.get('hash') for i in feed.get('images') or [] if i.get('hash')), '')
    )
    if image_hash:
        return f'image:{image_hash}'
    return f"creative:{creative['id']}" if creative.get('id') else ''


def _parse_ad_details(ad: dict) -> dict:
    """Normalize an ad's creative into display fields.

    Ad copy lives in different places depending on how the ad was built: the
    creative's own title/body, ``object_story_spec`` (link_data for image/link
    ads, video_data for video ads), or ``asset_feed_spec`` for dynamic
    creative, which can hold several headline/body variants.
    """
    creative = ad.get('creative') or {}
    story = creative.get('object_story_spec') or {}
    link_data = story.get('link_data') or {}
    video_data = story.get('video_data') or {}
    feed = creative.get('asset_feed_spec') or {}
    link_cta = link_data.get('call_to_action') or {}
    video_cta = video_data.get('call_to_action') or {}

    def feed_texts(key):
        return [item.get('text') for item in feed.get(key) or []]

    link_url = (
        link_data.get('link')
        or (video_cta.get('value') or {}).get('link')
        or (link_cta.get('value') or {}).get('link')
        or next((u.get('website_url') for u in feed.get('link_urls') or []), '')
    )
    cta = (
        creative.get('call_to_action_type')
        or link_cta.get('type')
        or video_cta.get('type')
        or next(iter(feed.get('call_to_action_types') or []), '')
    )
    descriptions = _unique([
        link_data.get('description'),
        video_data.get('link_description'),
        *feed_texts('descriptions'),
    ])
    object_type = creative.get('object_type') or ''
    return {
        'creative_key': _creative_key(creative),
        'is_video': bool(
            object_type == 'VIDEO' or video_data.get('video_id') or feed.get('videos')
        ),
        'image_url': _safe_url(creative.get('image_url') or creative.get('thumbnail_url')),
        'status': _humanize_enum(ad.get('effective_status') or ''),
        'is_active': ad.get('effective_status') == 'ACTIVE',
        'preview_url': _safe_url(ad.get('preview_shareable_link')),
        'format': _CREATIVE_FORMATS.get(object_type, _humanize_enum(object_type)),
        'headlines': _unique([
            creative.get('title'),
            link_data.get('name'),
            video_data.get('title'),
            *feed_texts('titles'),
        ]),
        'bodies': _unique([
            creative.get('body'),
            link_data.get('message'),
            video_data.get('message'),
            *feed_texts('bodies'),
        ]),
        'description': descriptions[0] if descriptions else '',
        'cta': _humanize_enum(cta),
        'link_url': _safe_url(link_url),
    }


class MetaAdsClient:
    """Small Graph API client for the Meta Ads endpoints Cue needs."""

    def __init__(self, access_token: str):
        self.access_token = access_token
        self.base_url = f"https://graph.facebook.com/{settings.FACEBOOK_GRAPH_API_VERSION}"

    def list_ad_accounts(self) -> list[dict]:
        return self._get_paginated(
            "/me/adaccounts",
            {"fields": "id,name,account_status,currency"},
            limit=200,
        )

    def get_user_profile(self) -> dict:
        return self._get("/me", {"fields": "id,name"})

    def list_campaigns(self, ad_account_id: str) -> list[dict]:
        account_id = self._normalize_account_id(ad_account_id)
        return self._get_paginated(
            f"/act_{account_id}/campaigns",
            {"fields": "id,name,objective,status,created_time,start_time,stop_time"},
            limit=200,
        )

    def get_campaign_insights(self, campaign_id: str) -> CampaignInsights:
        """Fetch lifetime spend plus attributed purchases/purchase value in one call."""
        payload = self._get(
            f"/{campaign_id}/insights",
            {"fields": "spend,actions,action_values", "date_preset": "maximum"},
        )
        rows = payload.get("data") or []
        if not rows:
            return CampaignInsights(spend=Decimal("0.00"), purchases=None, purchase_value=None)
        row = rows[0]
        try:
            spend = Decimal(str(row.get("spend") or "0")).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError):
            raise MetaAdsAPIError("Meta returned an invalid spend value.")
        return CampaignInsights(
            spend=spend,
            purchases=_extract_purchase_count(row.get("actions")),
            purchase_value=_extract_purchase_amount(row.get("action_values")),
        )

    def get_account_insights(self, ad_account_id: str, *, level: str, fields: str,
                             date_preset: str = '', time_range: tuple | None = None,
                             limit: int = 500, sort: str = '',
                             min_spend: Decimal | int | None = None) -> list[dict]:
        """Raw Insights rows for every campaign/adset/ad in the account at ``level``.

        Pass either a Meta ``date_preset`` (e.g. ``last_30d``) or an inclusive
        ``time_range`` of ``(since, until)`` dates. Meta only returns rows for
        objects that delivered in the window. Rows are unordered unless ``sort``
        is given (e.g. ``spend_descending``) — pass it whenever ``limit`` may
        truncate, so the cap drops the smallest rows. ``min_spend`` filters on
        Meta's side, so small rows never count toward ``limit``.
        """
        account_id = self._normalize_account_id(ad_account_id)
        params = {"level": level, "fields": fields}
        if time_range:
            since, until = time_range
            params["time_range"] = json.dumps({"since": since.isoformat(), "until": until.isoformat()})
        else:
            params["date_preset"] = date_preset
        if sort:
            params["sort"] = sort
        if min_spend:
            params["filtering"] = json.dumps([
                {"field": "spend", "operator": "GREATER_THAN_OR_EQUAL", "value": float(min_spend)},
            ])
        # Insights calls are what Meta rate-limits hardest, so use big pages.
        return self._get_paginated(
            f"/act_{account_id}/insights", params, limit=limit, page_size=min(limit, 500),
        )

    def get_ad_creative_keys(self, ad_account_id: str, ad_ids: list[str]) -> dict[str, str]:
        """Map ad id -> creative key (see ``_creative_key``); '' when it has none.

        Uses the account's ``/ads`` edge filtered to the ids (150 per call — far
        fewer calls than 50-id lookups, which matters on rate-limited tiers).
        That edge omits deleted/archived ads, so any ids it doesn't return are
        looked up directly.
        """
        account_id = self._normalize_account_id(ad_account_id)

        def fetch_chunk(chunk):
            return self._get_paginated(f"/act_{account_id}/ads", {
                "fields": f"id,{AD_CREATIVE_KEY_FIELDS}",
                "filtering": json.dumps([{"field": "id", "operator": "IN", "value": chunk}]),
            }, limit=len(chunk), page_size=len(chunk))

        keys = {}
        chunks = [ad_ids[start:start + 150] for start in range(0, len(ad_ids), 150)]
        # Independent lookups; a year of ads is several chunks, so overlap them.
        with ThreadPoolExecutor(max_workers=3) as pool:
            for ads in pool.map(fetch_chunk, chunks):
                for ad in ads:
                    keys[ad.get("id")] = _creative_key(ad.get("creative") or {})

        missing = [ad_id for ad_id in ad_ids if ad_id not in keys]
        for start in range(0, len(missing), 50):
            payload = self._get("/", {
                "ids": ",".join(missing[start:start + 50]),
                "fields": AD_CREATIVE_KEY_FIELDS,
            })
            for ad_id, ad in payload.items():
                keys[ad_id] = _creative_key((ad or {}).get("creative") or {})
        return keys

    def get_ad_details(self, ad_ids: list[str]) -> dict[str, dict]:
        """Map ad id -> creative image, copy, call to action, and status.

        Batched 50 ids per call (Meta's ``ids`` limit). See ``_parse_ad_details``
        for the returned shape.
        """
        def fetch_batch(batch):
            return self._get("/", {"ids": ",".join(batch), "fields": AD_DETAIL_FIELDS})

        details = {}
        batches = [ad_ids[start:start + 50] for start in range(0, len(ad_ids), 50)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            for payload in pool.map(fetch_batch, batches):
                details.update({ad_id: _parse_ad_details(ad or {}) for ad_id, ad in payload.items()})
        return details

    def get_ad_preview(self, ad_id: str) -> dict:
        """Return ``{'account_id', 'src'}`` for Meta's hosted ad preview player.

        The raw video files belong to the Facebook Page, which an ads_read token
        can't read, so playback goes through Meta's preview iframe instead. The
        ad's account id comes back in the same call so callers can verify
        ownership before exposing the preview.
        """
        payload = self._get(f"/{ad_id}", {
            "fields": f"account_id,previews.ad_format({AD_PREVIEW_FORMAT}){{body}}",
        })
        previews = (payload.get("previews") or {}).get("data") or []
        match = _PREVIEW_IFRAME_SRC.search((previews[0].get("body") or "") if previews else "")
        if not match:
            raise MetaAdsAPIError("Meta did not return a preview for this ad.")
        return {
            "account_id": str(payload.get("account_id") or ""),
            "src": html.unescape(match.group(1)),
        }

    def _get_paginated(self, path: str, params: dict, limit: int,
                       page_size: int = 100) -> list[dict]:
        url = f"{self.base_url}{path}"
        results = []
        next_params = {**params, "limit": min(limit, page_size)}

        while url and len(results) < limit:
            payload = self._request(url, next_params)
            results.extend(payload.get("data") or [])
            url = (payload.get("paging") or {}).get("next")
            next_params = None

        return results[:limit]

    def _get(self, path: str, params: dict | None = None) -> dict:
        return self._request(f"{self.base_url}{path}", params or {})

    def _request(self, url: str, params: dict | None = None, _retries: int = 1) -> dict:
        request_params = {} if "access_token=" in url else {"access_token": self.access_token}
        if params:
            request_params.update(params)
        try:
            response = requests.get(url, params=request_params, timeout=20)
        except requests.RequestException as exc:
            raise MetaAdsAPIError(f"Meta request failed: {exc}") from exc

        if response.status_code < 200 or response.status_code >= 300:
            error = _error_from_response(response)
            # GET is idempotent; Meta marks codes 1/2 as retryable, so give one
            # short-backoff retry before surfacing the failure to the caller.
            if error.is_transient and _retries > 0:
                time.sleep(0.5)
                return self._request(url, params, _retries=_retries - 1)
            raise error
        return response.json()

    @staticmethod
    def _normalize_account_id(ad_account_id: str) -> str:
        return str(ad_account_id).removeprefix("act_")


def exchange_code_for_token(code: str, redirect_uri: str) -> dict:
    return _oauth_request({
        "client_id": settings.FACEBOOK_APP_ID,
        "client_secret": settings.FACEBOOK_APP_SECRET,
        "redirect_uri": redirect_uri,
        "code": code,
    })


def exchange_for_long_lived_token(short_token: str) -> dict:
    return _oauth_request({
        "grant_type": "fb_exchange_token",
        "client_id": settings.FACEBOOK_APP_ID,
        "client_secret": settings.FACEBOOK_APP_SECRET,
        "fb_exchange_token": short_token,
    })


def _oauth_request(params: dict) -> dict:
    url = f"https://graph.facebook.com/{settings.FACEBOOK_GRAPH_API_VERSION}/oauth/access_token"
    try:
        response = requests.get(url, params=params, timeout=20)
    except requests.RequestException as exc:
        raise MetaAdsAPIError(f"Meta OAuth request failed: {exc}") from exc

    if response.status_code < 200 or response.status_code >= 300:
        raise _error_from_response(response)
    return response.json()


def _error_from_response(response) -> MetaAdsAPIError:
    """Build a diagnosable MetaAdsAPIError from a non-2xx Graph API response.

    The Graph API frequently returns a generic ``message`` (e.g. "An unknown
    error has occurred.") while hiding the real cause in ``code`` /
    ``error_subcode`` / ``error_user_msg`` / ``fbtrace_id``. We fold those into
    the exception string and log the full parsed payload so each failure is
    actionable instead of opaque.
    """
    status = response.status_code
    try:
        payload = response.json()
    except ValueError:
        payload = None

    error = (payload or {}).get("error") or {}
    code = error.get("code")
    subcode = error.get("error_subcode")
    fbtrace_id = error.get("fbtrace_id")
    detail = (
        error.get("error_user_msg")
        or error.get("message")
        or f"Meta API error ({status})."
    )

    code_bits = []
    if code is not None:
        code_bits.append(f"code {code}")
    if subcode:
        code_bits.append(f"subcode {subcode}")
    prefix = f"Meta API error ({', '.join(code_bits)})" if code_bits else f"Meta API error ({status})"
    message = f"{prefix}: {detail}"

    logger.warning(
        "Meta Graph API error: status=%s code=%s subcode=%s fbtrace_id=%s payload=%s",
        status, code, subcode, fbtrace_id, payload,
    )
    return MetaAdsAPIError(
        message,
        code=code,
        subcode=subcode,
        fbtrace_id=fbtrace_id,
        status=status,
        payload=payload,
    )
