"""Google API authentication via service account."""
import base64
import json
import logging
import os

from google.oauth2 import service_account
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/documents']


def get_credentials(credentials_source):
    """
    Build credentials from either a file path, base64-encoded JSON, or raw JSON string.

    Raises ValueError if credentials cannot be parsed.
    """
    # Try as file path first
    if os.path.isfile(credentials_source):
        logger.info("Loading Google credentials from file")
        return service_account.Credentials.from_service_account_file(
            credentials_source, scopes=SCOPES
        )

    # Try as base64-encoded JSON
    try:
        decoded = base64.b64decode(credentials_source)
        info = json.loads(decoded)
        logger.info("Loaded Google credentials from base64-encoded string")
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    except Exception:
        pass

    # Try as raw JSON string
    try:
        info = json.loads(credentials_source)
        logger.info("Loaded Google credentials from JSON string")
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    except Exception as e:
        raise ValueError(
            "Could not parse GOOGLE_SERVICE_ACCOUNT_JSON as a file path, "
            "base64-encoded JSON, or raw JSON string."
        ) from e


def get_google_docs_service(credentials_source=None):
    """
    Build and return a Google Docs API service client.

    Args:
        credentials_source: Optional override. Defaults to settings.GOOGLE_SERVICE_ACCOUNT_JSON.
    """
    if credentials_source is None:
        from django.conf import settings
        credentials_source = settings.GOOGLE_SERVICE_ACCOUNT_JSON

    if not credentials_source:
        raise ValueError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not configured. "
            "Set it as an environment variable."
        )

    credentials = get_credentials(credentials_source)
    return build('docs', 'v1', credentials=credentials)
