from datetime import timezone as py_timezone
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime


DEFAULT_BASE_URL = "https://dev.slicktext.com/v1"


class SlickTextAPIError(Exception):
    """Raised when SlickText API calls fail."""


class SlickTextClient:
    """Small direct client for the SlickText v2 REST API.

    Auth: Bearer token. Every endpoint (except /brands itself) is scoped to a brand_id.
    """

    def __init__(self, api_key: str, brand_id: str = '', timeout: int = 60):
        self.api_key = api_key
        self.brand_id = brand_id
        self.timeout = timeout
        self.base_url = getattr(settings, 'SLICKTEXT_API_BASE_URL', DEFAULT_BASE_URL).rstrip('/')

    def get_brand(self) -> dict:
        """Return the brand tied to this API key. Used for credential verification."""
        return self._request("GET", "/brands")

    def list_campaigns(self, limit: int = 200) -> list[dict]:
        if not self.brand_id:
            raise SlickTextAPIError("SlickText brand_id is required to list campaigns.")
        campaigns: list[dict] = []
        offset = 0
        page_size = min(250, max(1, limit))
        while len(campaigns) < limit:
            count = min(page_size, limit - len(campaigns))
            payload = self._request(
                "GET",
                f"/brands/{self.brand_id}/campaigns",
                params={"limit": count, "offset": offset},
            )
            page = _extract_list(payload, "campaigns")
            if not page:
                break
            campaigns.extend(page)
            offset += len(page)
            if len(page) < count:
                break
        return campaigns[:limit]

    def get_campaign(self, campaign_id: str) -> dict:
        if not self.brand_id:
            raise SlickTextAPIError("SlickText brand_id is required to fetch a campaign.")
        return self._request("GET", f"/brands/{self.brand_id}/campaigns/{campaign_id}")

    def get_campaign_analytics(self, campaign_id: str) -> dict:
        if not self.brand_id:
            raise SlickTextAPIError("SlickText brand_id is required to fetch campaign analytics.")
        return self._request("GET", f"/brands/{self.brand_id}/analytics/campaigns/{campaign_id}")

    def list_links(self, limit: int = 250, **filters) -> list[dict]:
        if not self.brand_id:
            raise SlickTextAPIError("SlickText brand_id is required to list links.")
        links: list[dict] = []
        offset = 0
        page_size = min(250, max(1, limit))
        while len(links) < limit:
            count = min(page_size, limit - len(links))
            params = {"limit": count, "offset": offset, **filters}
            payload = self._request(
                "GET",
                f"/brands/{self.brand_id}/links",
                params=params,
            )
            page = _extract_list(payload, "links")
            if not page:
                break
            links.extend(page)
            offset += len(page)
            if len(page) < count:
                break
        return links[:limit]

    def get_campaign_links(self, campaign_id: str) -> list[dict]:
        if not campaign_id:
            return []
        return self.list_links(source="Campaign", _source_id=str(campaign_id))

    def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        try:
            response = requests.request(
                method,
                url,
                params=params,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SlickTextAPIError(f"SlickText request failed: {exc}") from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise SlickTextAPIError(_extract_error_message(response))
        try:
            return response.json()
        except ValueError as exc:
            raise SlickTextAPIError("SlickText returned an invalid JSON response.") from exc


def build_campaign_report(campaign: dict, analytics: dict | None, links: list[dict] | None = None) -> dict:
    """Combine a campaign record, analytics, and campaign links for normalization."""
    return {
        'campaign': campaign or {},
        'analytics': analytics or {},
        'links': links or [],
    }


def normalize_campaign_report(report: dict) -> dict:
    """Flatten a SlickText campaign report into the EventSMSCampaign field shape."""
    campaign = report.get('campaign') or {}
    analytics = report.get('analytics') or {}
    analytics_totals = analytics.get('totals') if isinstance(analytics, dict) else {}
    analytics_totals = analytics_totals if isinstance(analytics_totals, dict) else {}
    analytics_campaign = analytics.get('campaign') if isinstance(analytics, dict) else {}
    analytics_campaign = analytics_campaign if isinstance(analytics_campaign, dict) else {}
    links = report.get('links') or []
    link_metrics = _sum_link_metrics(links)

    send_time = (
        _parse_datetime(campaign.get('finished'))
        or _parse_datetime(analytics_campaign.get('finished'))
        or _parse_datetime(campaign.get('started'))
        or _parse_datetime(analytics_campaign.get('started'))
        or _parse_datetime(campaign.get('scheduled'))
        or _parse_datetime(analytics_campaign.get('scheduled'))
        or _parse_datetime(campaign.get('status_date'))
        or _parse_datetime(analytics_campaign.get('status_date'))
    )

    clicks = _to_int(_metric_value(analytics_totals, analytics, 'clicks'))
    unique_clicks = _to_int(_metric_value(analytics_totals, analytics, 'unique_clicks'))
    if clicks == 0 and link_metrics['clicks'] > 0:
        clicks = link_metrics['clicks']
    if unique_clicks == 0 and link_metrics['unique_clicks'] > 0:
        unique_clicks = link_metrics['unique_clicks']

    return {
        'external_id': str(
            campaign.get('campaign_id')
            or campaign.get('id')
            or analytics_campaign.get('campaign_id')
            or analytics_campaign.get('id')
            or ''
        ),
        'name': str(campaign.get('name') or analytics_campaign.get('name') or '')[:300],
        'message': str(
            campaign.get('body')
            or campaign.get('message')
            or analytics_campaign.get('body')
            or analytics_campaign.get('message')
            or ''
        )[:1600],
        'media_url': str(campaign.get('media_url') or analytics_campaign.get('media_url') or '')[:500],
        'send_time': send_time,
        'audience_size': _to_int(
            _metric_value(analytics_totals, analytics, 'contacts')
            or campaign.get('audience_size')
            or analytics_campaign.get('audience_size')
        ),
        'clicks': clicks,
        'unique_clicks': unique_clicks,
        'click_rate': _to_decimal(_metric_value(analytics_totals, analytics, 'click_rate'), "0.0000"),
        'unsubscribes': _to_int(_metric_value(analytics_totals, analytics, 'unsubscribes')),
        'unsubscribe_rate': _to_decimal(_metric_value(analytics_totals, analytics, 'unsubscribe_rate'), "0.0000"),
        'orders': _to_int(_metric_value(analytics_totals, analytics, 'orders')),
        'revenue': _to_decimal(_metric_value(analytics_totals, analytics, 'revenue'), "0.00"),
        'external_metadata': {
            'status': campaign.get('status') or analytics_campaign.get('status'),
            'status_detail': campaign.get('status_detail') or analytics_campaign.get('status_detail'),
            'completion_percentage': (
                campaign.get('completion_percentage')
                if campaign.get('completion_percentage') is not None
                else analytics_campaign.get('completion_percentage')
            ),
            'created': campaign.get('created') or analytics_campaign.get('created'),
            'last_updated': campaign.get('last_updated') or analytics_campaign.get('last_updated'),
            'link_metrics': link_metrics,
            'raw_campaign': campaign,
            'raw_analytics': analytics,
            'raw_links': links,
        },
    }


def _extract_list(payload, key: str):
    """SlickText sometimes wraps lists in {key: [...]}, sometimes returns a bare list."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for candidate in (key, 'data', 'results', 'items'):
            value = payload.get(candidate)
            if isinstance(value, list):
                return value
    return []


def _metric_value(primary: dict, fallback: dict, key: str):
    value = primary.get(key)
    if value is not None:
        return value
    return fallback.get(key)


def _sum_link_metrics(links: list[dict]) -> dict:
    return {
        'clicks': sum(_to_int(link.get('clicks')) for link in links if isinstance(link, dict)),
        'unique_clicks': sum(_to_int(link.get('unique_clicks')) for link in links if isinstance(link, dict)),
        'bot_clicks': sum(_to_int(link.get('bot_clicks')) for link in links if isinstance(link, dict)),
    }


def _parse_datetime(value):
    if not value:
        return None
    parsed = parse_datetime(str(value))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, py_timezone.utc)
    return parsed


def _to_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _to_decimal(value, default: str) -> Decimal:
    try:
        return Decimal(str(value if value is not None else default)).quantize(Decimal(default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _extract_error_message(response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"SlickText API error ({response.status_code})."

    if isinstance(payload, dict):
        for key in ('detail', 'message', 'error_description', 'error', 'title'):
            value = payload.get(key)
            if value:
                return str(value)
    return f"SlickText API error ({response.status_code})."
