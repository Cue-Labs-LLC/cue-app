"""Inbound Meta webhook normalization + signature verification for the IG DM agent.

Meta delivers every subscribed event for the app to one webhook URL. This module turns
a raw payload into a flat list of ``NormalizedInbound`` (one per real inbound text DM)
and verifies the ``X-Hub-Signature-256`` HMAC. The view (``instagram_webhook``) stays
thin: verify → normalize → resolve org → enqueue.

Payload shape (Instagram messaging)::

    {"object": "instagram",
     "entry": [{"id": "<IG account id>", "time": ...,
                "messaging": [{"sender": {"id": "<customer IGSID>"},
                               "recipient": {"id": "<IG account id>"},
                               "timestamp": ...,
                               "message": {"mid": "...", "text": "hi"}}]}]}

Org resolution keys on ``entry[].id`` — the account id that actually appears in the
webhook. Phase 7's OAuth must persist THAT id into
``Organization.instagram_business_account_id`` (it is the IGSID Meta sends here, which
can differ from a Graph business-account id). Resolving on anything else risks every
production message silently 200-no-op'ing.
"""

import hashlib
import hmac
import logging
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass
class NormalizedInbound:
    """One inbound text DM, flattened from the Meta payload."""

    ig_account_id: str      # entry[].id — the org's IG account (routing key)
    sender_id: str          # messaging[].sender.id — the customer; our reply recipient
    text: str
    provider_message_id: str
    timestamp: int = 0


def _event_text(messaging: dict) -> str:
    """Extract reply-able text from one messaging event, or '' to skip.

    Keeps story replies / story-mentions that carry ``message.text`` (a dominant way
    audiences open event DMs) — they arrive with text PLUS an attachment, so we read
    the text rather than lumping them into the non-text skip. Skips echoes (our own
    outbound), reactions, read receipts, and attachment-only messages.
    """
    message = messaging.get('message')
    if not isinstance(message, dict):
        return ''  # delivery/read receipt, reaction, postback, etc.
    if message.get('is_echo'):
        return ''  # our own outbound echoed back
    text = message.get('text')
    if not isinstance(text, str):
        return ''
    return text.strip()


def normalize_meta_payload(body: dict) -> list:
    """Flatten a Meta webhook body into a list of ``NormalizedInbound``."""
    if not isinstance(body, dict):
        return []
    out = []
    for entry in body.get('entry', []) or []:
        if not isinstance(entry, dict):
            continue
        ig_account_id = str(entry.get('id', '') or '')
        for messaging in entry.get('messaging', []) or []:
            if not isinstance(messaging, dict):
                continue
            text = _event_text(messaging)
            if not text:
                continue
            sender_id = str((messaging.get('sender') or {}).get('id', '') or '')
            message = messaging.get('message') or {}
            provider_message_id = str(message.get('mid', '') or '')
            if not sender_id or not ig_account_id:
                continue
            out.append(NormalizedInbound(
                ig_account_id=ig_account_id,
                sender_id=sender_id,
                text=text,
                provider_message_id=provider_message_id,
                timestamp=int(messaging.get('timestamp', 0) or 0),
            ))
    return out


def verify_meta_signature(request) -> bool:
    """Validate the ``X-Hub-Signature-256`` HMAC on an inbound Meta webhook POST.

    Meta signs with a hex HMAC-SHA256 of the raw body under the app secret
    (``sha256=<hexdigest>``). Bypassed in E2E test mode or when
    INSTAGRAM_VALIDATE_WEBHOOKS is False (local dev without a tunnel) — mirrors
    ``validate_twilio_request``.
    """
    if getattr(settings, 'E2E_TEST_MODE', False):
        return True
    if not getattr(settings, 'INSTAGRAM_VALIDATE_WEBHOOKS', True):
        return True
    app_secret = getattr(settings, 'FACEBOOK_APP_SECRET', '') or ''
    header = request.headers.get('X-Hub-Signature-256', '')
    if not app_secret or not header.startswith('sha256='):
        return False
    posted_hex = header[len('sha256='):]
    expected_hex = hmac.new(
        app_secret.encode('utf-8'), request.body, hashlib.sha256,
    ).hexdigest()
    try:
        return hmac.compare_digest(posted_hex, expected_hex)
    except (TypeError, ValueError):
        return False
