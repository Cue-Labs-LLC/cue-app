import logging
import time
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

    def _get_paginated(self, path: str, params: dict, limit: int) -> list[dict]:
        url = f"{self.base_url}{path}"
        results = []
        next_params = {**params, "limit": min(limit, 100)}

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
