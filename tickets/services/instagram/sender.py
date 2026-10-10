"""Outbound transport for the Instagram DM support agent.

Transport-agnostic by design: the orchestration task talks to an ``InstagramSender``
interface, never to Meta directly. Phase 3 ships only ``StubSender`` (logs, no network)
so the full inbound→answer→send loop is demoable before Meta App Review clears; Phase 7
adds a real ``GraphAPISender`` as a second backend without touching the task.

``get_sender`` selects the backend by ``settings.INSTAGRAM_SENDER_BACKEND`` — NOT by
whether a token is present. A factory that fell back to the stub when creds were missing
would turn a production token expiry into silent success: messages marked ``auto_sent``
while the customer receives nothing (D10).
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass
class SendResult:
    """Outcome of one outbound send."""

    ok: bool
    provider_message_id: str = ''
    error: str = ''


class InstagramSender(ABC):
    """Sends one text DM to an Instagram user.

    Contract note for Phase 7: Meta enforces a 24-hour standard messaging window;
    replies outside it require the ``human_agent`` message tag (a gated, 7-day window).
    The stub ignores this, but the interface names it so the Phase 4 human-review path
    isn't designed against an impossible assumption — a real sender surfaces an
    out-of-window rejection as ``SendResult(ok=False, ...)``.
    """

    @abstractmethod
    def send_text(self, recipient_id: str, text: str, *, human_agent: bool = False) -> SendResult:
        """Send one text DM.

        ``human_agent=True`` marks the message as a human reply (``HUMAN_AGENT`` tag,
        7-day window) rather than an automated in-window response — used by the inbox
        reply/approve paths. Auto-answers leave it False (``RESPONSE``, 24h window).
        """
        ...


class StubSender(InstagramSender):
    """No-op sender for demos and tests: logs and reports success, sends nothing."""

    def send_text(self, recipient_id: str, text: str, *, human_agent: bool = False) -> SendResult:
        import uuid

        provider_message_id = f"stub-{uuid.uuid4().hex[:24]}"
        logger.info(
            "StubSender: would send to IG user %s (%d chars, human_agent=%s), mid=%s",
            recipient_id, len(text or ''), human_agent, provider_message_id,
        )
        return SendResult(ok=True, provider_message_id=provider_message_id)


class UnavailableSender(InstagramSender):
    """Fails loudly for a configured-but-unavailable backend (e.g. 'graph' before P7,
    or 'graph' with missing/expired creds). Never silently succeeds (D10)."""

    def __init__(self, reason):
        self.reason = reason

    def send_text(self, recipient_id: str, text: str, *, human_agent: bool = False) -> SendResult:
        logger.error("Instagram send backend unavailable: %s", self.reason)
        return SendResult(ok=False, error=self.reason)


def get_sender(organization) -> InstagramSender:
    """Return the sender for the configured backend (``INSTAGRAM_SENDER_BACKEND``).

    Selection is by setting, NEVER by token presence (D10). Under the ``graph`` backend a
    missing/expired credential yields an ``UnavailableSender`` (loud ``failed``), not a
    silent fall-back to ``StubSender`` that would mark messages ``auto_sent`` while the
    customer receives nothing.
    """
    backend = getattr(settings, 'INSTAGRAM_SENDER_BACKEND', 'stub')
    if backend == 'stub':
        return StubSender()
    if backend == 'graph':
        if organization and organization.instagram_page_access_token and organization.instagram_business_account_id:
            # Imported lazily so the module has no hard requests/graph dependency when
            # running on the stub backend (and to avoid any import cycle).
            from .graph_client import GraphAPISender
            return GraphAPISender(organization)
        return UnavailableSender(
            "INSTAGRAM_SENDER_BACKEND='graph' but this organization has no Instagram "
            "credentials — reconnect Instagram in settings."
        )
    return UnavailableSender(f"Unknown INSTAGRAM_SENDER_BACKEND={backend!r}.")
