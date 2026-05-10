import base64
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import requests
from django.conf import settings


class PipedreamConnectError(Exception):
    """Raised when Pipedream Connect returns an error response."""


def external_user_id_for_org(organization) -> str:
    return f"org:{organization.id}"


class PipedreamConnectClient:
    """Minimal REST client for Pipedream Connect."""

    API_BASE = "https://api.pipedream.com/v1"

    def __init__(self):
        self.project_id = settings.PIPEDREAM_PROJECT_ID
        self.environment = settings.PIPEDREAM_ENVIRONMENT

    def create_oauth_token(self, scope: str = "*") -> str:
        payload = self._request(
            "POST",
            f"{self.API_BASE}/oauth/token",
            json={
                "grant_type": "client_credentials",
                "client_id": settings.PIPEDREAM_CLIENT_ID,
                "client_secret": settings.PIPEDREAM_CLIENT_SECRET,
                "scope": scope,
            },
            include_environment=False,
            include_auth=False,
        )
        try:
            return payload["access_token"]
        except KeyError as exc:
            raise PipedreamConnectError("Pipedream did not return an OAuth access token.") from exc

    def create_connect_token(self, external_user_id: str, success_redirect_uri: str, error_redirect_uri: str) -> dict:
        access_token = self.create_oauth_token("connect:tokens:create")
        return self._request(
            "POST",
            self._connect_url("/tokens"),
            json={
                "external_user_id": external_user_id,
                "success_redirect_uri": success_redirect_uri,
                "error_redirect_uri": error_redirect_uri,
                "scope": "connect:accounts:read connect:accounts:write",
            },
            access_token=access_token,
        )

    def build_connect_link(self, connect_token_payload: dict, app_slug: str) -> str:
        base_url = connect_token_payload.get("connect_link_url")
        if not base_url:
            raise PipedreamConnectError("Pipedream did not return a Connect Link URL.")
        return _merge_query_params(base_url, {"connectLink": "true", "app": app_slug})

    def list_accounts(self, external_user_id: str, app_slug: str) -> list[dict]:
        access_token = self.create_oauth_token("connect:accounts:read")
        payload = self._request(
            "GET",
            self._connect_url("/accounts"),
            params={
                "external_user_id": external_user_id,
                "app": app_slug,
                "limit": 100,
            },
            access_token=access_token,
        )
        return payload.get("data") or payload.get("accounts") or []

    def delete_account(self, account_id: str) -> None:
        access_token = self.create_oauth_token("connect:accounts:write")
        self._request(
            "DELETE",
            self._connect_url(f"/accounts/{account_id}"),
            access_token=access_token,
            expect_json=False,
        )

    def proxy(self, method: str, external_user_id: str, account_id: str, target_url: str, params: dict | None = None) -> dict:
        access_token = self.create_oauth_token("connect:proxy")
        url = self._connect_url(f"/proxy/{_url64(target_url)}")
        return self._request(
            method,
            url,
            params={
                "external_user_id": external_user_id,
                "account_id": account_id,
                **(params or {}),
            },
            access_token=access_token,
        )

    def run_action(self, external_user_id: str, component_id: str, configured_props: dict) -> dict:
        access_token = self.create_oauth_token("*")
        return self._request(
            "POST",
            self._connect_url("/actions/run"),
            json={
                "id": component_id,
                "external_user_id": external_user_id,
                "configured_props": configured_props,
            },
            access_token=access_token,
        )

    def _connect_url(self, path: str) -> str:
        return f"{self.API_BASE}/connect/{self.project_id}{path}"

    def _request(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        json: dict | None = None,
        access_token: str | None = None,
        include_environment: bool = True,
        include_auth: bool = True,
        expect_json: bool = True,
    ):
        headers = {"Content-Type": "application/json"}
        if include_environment:
            headers["x-pd-environment"] = self.environment
        if include_auth:
            headers["Authorization"] = f"Bearer {access_token}"
        try:
            response = requests.request(
                method,
                url,
                params=params,
                json=json,
                headers=headers,
                timeout=30,
            )
        except requests.RequestException as exc:
            raise PipedreamConnectError(f"Pipedream request failed: {exc}") from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise PipedreamConnectError(_extract_error_message(response))
        if not expect_json or response.status_code == 204:
            return None
        return response.json()


class PipedreamMailchimpClient:
    """Mailchimp report client backed by Pipedream Connect Proxy."""

    def __init__(self, connection, connect_client: PipedreamConnectClient | None = None):
        self.connection = connection
        self.connect_client = connect_client or PipedreamConnectClient()

    def list_campaign_reports(self, limit: int = 200) -> list[dict]:
        payload = self._run_action(
            "mailchimp-search-campaign",
            {"query": "*"},
        )
        results = ((payload.get("ret") or {}).get("results") or [])
        reports = []
        for item in results:
            campaign = item.get("campaign") or item
            report = _campaign_to_report(campaign)
            if report["send_time"]:
                reports.append(report)
        return reports[:limit]

    def get_campaign_report(self, campaign_id: str) -> dict:
        payload = self._run_action(
            "mailchimp-get-campaign-report",
            {"campaignId": campaign_id},
        )
        return payload.get("ret") or {}

    def _proxy_get(self, target_url: str, params: dict | None = None) -> dict:
        return self.connect_client.proxy(
            "GET",
            self.connection.external_user_id,
            self.connection.account_id,
            target_url,
            params=params,
        )

    def _run_action(self, component_id: str, configured_props: dict) -> dict:
        return self.connect_client.run_action(
            self.connection.external_user_id,
            component_id,
            {
                "mailchimp": {"authProvisionId": self.connection.account_id},
                **configured_props,
            },
        )


def account_display_name(account: dict) -> str:
    app = account.get("app") or {}
    return (
        account.get("name")
        or account.get("account_name")
        or app.get("name")
        or account.get("id")
        or ""
    )


def _campaign_to_report(campaign: dict) -> dict:
    settings_payload = campaign.get("settings") or {}
    recipients = campaign.get("recipients") or {}
    return {
        "id": campaign.get("id"),
        "campaign_title": settings_payload.get("title") or campaign.get("campaign_title") or campaign.get("id"),
        "subject_line": settings_payload.get("subject_line") or campaign.get("subject_line") or "",
        "type": campaign.get("type"),
        "emails_sent": campaign.get("emails_sent") or 0,
        "send_time": campaign.get("send_time") or "",
        "archive_url": campaign.get("long_archive_url") or campaign.get("archive_url") or "",
        "list_id": recipients.get("list_id") or campaign.get("list_id"),
        "list_name": recipients.get("list_name") or campaign.get("list_name"),
        "status": campaign.get("status"),
        "raw_campaign": campaign,
    }


def _url64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _merge_query_params(url: str, params: dict) -> str:
    split = urlsplit(url)
    query = dict(parse_qsl(split.query, keep_blank_values=True))
    query.update(params)
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), split.fragment))


def _extract_error_message(response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"Pipedream API error ({response.status_code})."
    return (
        payload.get("message")
        or payload.get("error")
        or payload.get("detail")
        or f"Pipedream API error ({response.status_code})."
    )
