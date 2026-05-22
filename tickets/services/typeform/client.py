"""Typeform REST API client (Personal Access Token auth)."""

from datetime import datetime, timezone as dt_timezone

import requests
from django.conf import settings


DEFAULT_BASE_URL = 'https://api.typeform.com'


class TypeformAPIError(Exception):
    """Raised when a Typeform API request fails."""

    def __init__(self, message, status_code=None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class TypeformClient:
    """Direct client for the Typeform REST API.

    Auth: Bearer token (Personal Access Token for v1).
    """

    def __init__(self, access_token: str, timeout: int = 30):
        self.access_token = access_token
        self.timeout = timeout
        self.base_url = getattr(settings, 'TYPEFORM_API_BASE', DEFAULT_BASE_URL).rstrip('/')

    def validate_token(self) -> dict:
        """GET /me — used to verify credentials and grab the account email."""
        return self._request('GET', '/me')

    def list_forms(self, page_size: int = 200) -> list[dict]:
        forms: list[dict] = []
        page = 1
        while True:
            payload = self._request(
                'GET', '/forms',
                params={'page': page, 'page_size': min(page_size, 200)},
            )
            items = payload.get('items') or []
            forms.extend(items)
            if len(items) < page_size or len(forms) >= (payload.get('total_items') or 0):
                break
            page += 1
        return forms

    def get_form(self, form_id: str) -> dict:
        return self._request('GET', f'/forms/{form_id}')

    def list_responses(
        self,
        form_id: str,
        since=None,
        until=None,
        after: str | None = None,
        page_size: int = 200,
    ) -> dict:
        """GET /forms/{id}/responses. `since`/`until` accept ISO datetime; `after` is a token cursor."""
        params: dict = {'page_size': min(page_size, 1000)}
        if since:
            params['since'] = _format_typeform_datetime(since)
        if until:
            params['until'] = _format_typeform_datetime(until)
        if after:
            params['after'] = after
        return self._request('GET', f'/forms/{form_id}/responses', params=params)

    def create_webhook(self, form_id: str, url: str, secret: str, tag: str = 'cue') -> dict:
        return self._request(
            'PUT',
            f'/forms/{form_id}/webhooks/{tag}',
            json={
                'url': url,
                'enabled': True,
                'secret': secret,
                'verify_ssl': True,
            },
        )

    def delete_webhook(self, form_id: str, tag: str = 'cue') -> None:
        self._request('DELETE', f'/forms/{form_id}/webhooks/{tag}', expect_json=False)

    def _request(self, method: str, path: str, params: dict | None = None,
                 json: dict | None = None, expect_json: bool = True):
        url = f'{self.base_url}{path}'
        headers = {
            'Accept': 'application/json',
            'Authorization': f'Bearer {self.access_token}',
        }
        if json is not None:
            headers['Content-Type'] = 'application/json'
        try:
            response = requests.request(
                method, url,
                params=params, json=json,
                headers=headers, timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise TypeformAPIError(f'Typeform request failed: {exc}') from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise TypeformAPIError(
                _extract_error_message(response),
                status_code=response.status_code,
                body=response.text,
            )

        if not expect_json or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise TypeformAPIError('Typeform returned an invalid JSON response.') from exc


def _format_typeform_datetime(value) -> str:
    """Typeform's since/until params accept only `YYYY-MM-DDTHH:MM:SSZ` (UTC, no offset, no fractional)
    or an integer epoch. Anything else (e.g. `+00:00` offset, microseconds) is rejected with HTTP 400.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt_timezone.utc)
        return value.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return str(value)


def _extract_error_message(response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f'Typeform API error ({response.status_code}).'

    if isinstance(payload, dict):
        for key in ('description', 'detail', 'message', 'error_description', 'error'):
            value = payload.get(key)
            if value:
                return f'Typeform API error ({response.status_code}): {value}'
    return f'Typeform API error ({response.status_code}).'
