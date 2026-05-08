from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings


class MetaAdsAPIError(Exception):
    """Raised when the Meta Graph API returns an error response."""


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

    def get_campaign_spend(self, campaign_id: str) -> Decimal:
        payload = self._get(
            f"/{campaign_id}/insights",
            {"fields": "spend", "date_preset": "maximum"},
        )
        rows = payload.get("data") or []
        if not rows:
            return Decimal("0.00")
        try:
            return Decimal(str(rows[0].get("spend") or "0")).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError):
            raise MetaAdsAPIError("Meta returned an invalid spend value.")

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

    def _request(self, url: str, params: dict | None = None) -> dict:
        request_params = {} if "access_token=" in url else {"access_token": self.access_token}
        if params:
            request_params.update(params)
        try:
            response = requests.get(url, params=request_params, timeout=20)
        except requests.RequestException as exc:
            raise MetaAdsAPIError(f"Meta request failed: {exc}") from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise MetaAdsAPIError(_extract_error_message(response))
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
        raise MetaAdsAPIError(_extract_error_message(response))
    return response.json()


def _extract_error_message(response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"Meta API error ({response.status_code})."
    error = payload.get("error") or {}
    return error.get("message") or f"Meta API error ({response.status_code})."
