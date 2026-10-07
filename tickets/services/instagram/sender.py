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
    def send_text(self, recipient_id: str, text: str) -> SendResult:
        ...


class StubSender(InstagramSender):
    """No-op sender for demos and tests: logs and reports success, sends nothing."""

    def send_text(self, recipient_id: str, text: str) -> SendResult:
        import uuid

        provider_message_id = f"stub-{uuid.uuid4().hex[:24]}"
        logger.info(
            "StubSender: would send to IG user %s (%d chars), mid=%s",
            recipient_id, len(text or ''), provider_message_id,
        )
        return SendResult(ok=True, provider_message_id=provider_message_id)


class UnavailableSender(InstagramSender):
    """Fails loudly for a configured-but-unavailable backend (e.g. 'graph' before P7,
    or 'graph' with missing/expired creds). Never silently succeeds (D10)."""

    def __init__(self, reason):
        self.reason = reason

    def send_text(self, recipient_id: str, text: str) -> SendResult:
        logger.error("Instagram send backend unavailable: %s", self.reason)
        return SendResult(ok=False, error=self.reason)


def get_sender(organization) -> InstagramSender:
    """Return the sender for the configured backend (``INSTAGRAM_SENDER_BACKEND``)."""
    backend = getattr(settings, 'INSTAGRAM_SENDER_BACKEND', 'stub')
    if backend == 'stub':
        return StubSender()
    if backend == 'graph':
        # The real GraphAPISender lands in Phase 7 (gated by Meta App Review). Until
        # then a 'graph' backend is a misconfiguration — fail loudly, don't stub-send.
        return UnavailableSender(
            "INSTAGRAM_SENDER_BACKEND='graph' but the Graph sender is not available "
            "yet (Phase 7). Set INSTAGRAM_SENDER_BACKEND='stub'."
        )
    return UnavailableSender(f"Unknown INSTAGRAM_SENDER_BACKEND={backend!r}.")
