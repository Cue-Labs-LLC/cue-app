"""Fan an APNs payload out to every device token in an organization.

Mirrors ``tickets/services/webhooks/dispatch.py``: never raises into the
caller's request path, and defers enqueue until the surrounding transaction
commits so a rolled-back write never fires a push.
"""

import logging

from django.db import transaction

from .payloads import TAP_TO_PAY_ENABLED

logger = logging.getLogger(__name__)


def dispatch(organization, payload):
    """Enqueue one send task per device token registered to ``organization``."""
    from tickets.models import DeviceToken
    from tickets.tasks import send_push_notification_task

    try:
        token_ids = list(
            DeviceToken.objects.filter(organization=organization).values_list('id', flat=True)
        )
    except Exception:
        logger.exception("Push dispatch failed to load device tokens for org=%s",
                         getattr(organization, 'id', None))
        return

    for token_id in token_ids:
        try:
            send_push_notification_task.delay(str(token_id), payload)
        except Exception:
            logger.error(
                "Push enqueue failed (delivery lost) org=%s device_token=%s",
                getattr(organization, 'id', None), token_id, exc_info=True,
            )


def fire_tap_to_pay_enabled(organization):
    """Notify an org's devices that Tap to Pay just went live."""
    transaction.on_commit(lambda: dispatch(organization, TAP_TO_PAY_ENABLED))
