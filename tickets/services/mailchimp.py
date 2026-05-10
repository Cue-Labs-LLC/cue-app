from datetime import timezone as py_timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime


AUTHORIZE_URL = "https://login.mailchimp.com/oauth2/authorize"
TOKEN_URL = "https://login.mailchimp.com/oauth2/token"
METADATA_URL = "https://login.mailchimp.com/oauth2/metadata"


class MailchimpAPIError(Exception):
    """Raised when Mailchimp OAuth or Marketing API calls fail."""


def build_authorize_url(redirect_uri: str, state: str) -> str:
    params = {
        'response_type': 'code',
        'client_id': settings.MAILCHIMP_CLIENT_ID,
        'redirect_uri': redirect_uri,
        'state': state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code_for_token(code: str, redirect_uri: str) -> str:
    payload = {
        "grant_type": "authorization_code",
        "client_id": settings.MAILCHIMP_CLIENT_ID,
        "client_secret": settings.MAILCHIMP_CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "code": code,
    }
    data = _request_oauth("POST", TOKEN_URL, data=payload)
    access_token = data.get("access_token")
    if not access_token:
        raise MailchimpAPIError("Mailchimp did not return an access token.")
    return access_token


def get_oauth_metadata(access_token: str) -> dict:
    return _request_oauth(
        "GET",
        METADATA_URL,
        headers={"Authorization": f"OAuth {access_token}"},
    )


class MailchimpClient:
    """Small direct client for Mailchimp Marketing API report endpoints."""

    def __init__(self, access_token: str, dc: str, timeout: int = 30):
        self.access_token = access_token
        self.dc = dc
        self.timeout = timeout
        self.base_url = f"https://{dc}.api.mailchimp.com/3.0"

    def get_account_root(self) -> dict:
        return self._request("GET", "/")

    def list_campaign_reports(self, limit: int = 200) -> list[dict]:
        reports = []
        offset = 0
        page_size = min(100, max(1, limit))
        while len(reports) < limit:
            count = min(page_size, limit - len(reports))
            payload = self._request("GET", "/reports", params={"count": count, "offset": offset})
            page = payload.get("reports") or []
            reports.extend(page)
            offset += len(page)
            total_items = payload.get("total_items")
            if not page or len(page) < count or (total_items is not None and offset >= total_items):
                break
        return reports[:limit]

    def get_campaign_report(self, campaign_id: str) -> dict:
        return self._request("GET", f"/reports/{campaign_id}")

    def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.access_token}",
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
            raise MailchimpAPIError(f"Mailchimp request failed: {exc}") from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise MailchimpAPIError(_extract_error_message(response))
        try:
            return response.json()
        except ValueError as exc:
            raise MailchimpAPIError("Mailchimp returned an invalid JSON response.") from exc


def normalize_campaign_report(report: dict) -> dict:
    bounces = report.get("bounces") or {}
    opens = report.get("opens") or {}
    clicks = report.get("clicks") or {}
    ecommerce = report.get("ecommerce") or {}
    send_time = _parse_datetime(report.get("send_time"))

    return {
        "external_id": str(report.get("id") or ""),
        "campaign_title": str(report.get("campaign_title") or report.get("title") or report.get("id") or ""),
        "subject_line": str(report.get("subject_line") or ""),
        "send_time": send_time,
        "archive_url": str(report.get("archive_url") or ""),
        "emails_sent": _to_int(report.get("emails_sent")),
        "opens": _to_int(opens.get("opens_total")),
        "unique_opens": _to_int(opens.get("unique_opens")),
        "open_rate": _to_decimal(opens.get("open_rate"), "0.0000"),
        "clicks": _to_int(clicks.get("clicks_total")),
        "unique_clicks": _to_int(clicks.get("unique_clicks")),
        "click_rate": _to_decimal(clicks.get("click_rate"), "0.0000"),
        "bounces": (
            _to_int(bounces.get("hard_bounces"))
            + _to_int(bounces.get("soft_bounces"))
            + _to_int(bounces.get("syntax_errors"))
        ),
        "unsubscribes": _to_int(report.get("unsubscribed")),
        "abuse_reports": _to_int(report.get("abuse_reports")),
        "ecommerce_orders": _to_int(ecommerce.get("total_orders") or ecommerce.get("orders")),
        "ecommerce_revenue": _to_decimal(
            ecommerce.get("total_revenue") or ecommerce.get("total_spent"),
            "0.00",
        ),
        "external_metadata": {
            "type": report.get("type"),
            "list_id": report.get("list_id"),
            "list_name": report.get("list_name"),
            "raw_report": report,
        },
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


def _request_oauth(method: str, url: str, data: dict | None = None, headers: dict | None = None) -> dict:
    try:
        response = requests.request(
            method,
            url,
            data=data,
            headers=headers or {},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise MailchimpAPIError(f"Mailchimp OAuth request failed: {exc}") from exc

    if response.status_code < 200 or response.status_code >= 300:
        raise MailchimpAPIError(_extract_error_message(response))
    try:
        return response.json()
    except ValueError as exc:
        raise MailchimpAPIError("Mailchimp returned an invalid JSON response.") from exc


def _extract_error_message(response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"Mailchimp API error ({response.status_code})."

    return (
        payload.get("detail")
        or payload.get("message")
        or payload.get("error_description")
        or payload.get("error")
        or payload.get("title")
        or f"Mailchimp API error ({response.status_code})."
    )
