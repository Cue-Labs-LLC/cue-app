"""Dispatch entrypoint for outbound webhooks.

`dispatch(event_type, organization, payload)` finds every active endpoint in the
org subscribed to `event_type` and enqueues one delivery task per endpoint. It
never raises into the caller's request path — a webhook must never break the
operation that triggered it (mirrors `_sync_event_to_google_calendar`).

Call sites inside a `transaction.atomic()` block should wrap this in
`transaction.on_commit(...)` so a rolled-back write never fires a webhook.
"""

import logging
import uuid

from django.db import transaction

from .constants import EVENT_CREATED, ORDER_CREATED, CUSTOMER_CREATED
from .payloads import (
    build_event_payload,
    build_order_payload,
    build_customer_payload,
)

logger = logging.getLogger(__name__)


def dispatch(event_type, organization, payload):
    """Enqueue a signed delivery for every active endpoint subscribed to event_type.

    A fresh `delivery_id` is minted per (endpoint, event) and passed to the task so
    every retry of that delivery carries the same id — consumers use it as an
    at-least-once dedupe key. Enqueue is guarded per-endpoint so one broker/publish
    failure can't drop the others, and a failure is logged at error level (a
    durable transactional outbox is a tracked follow-up — see TODOS.md).
    """
    from tickets.models import WebhookEndpoint
    from tickets.tasks import deliver_webhook_task

    try:
        endpoints = list(
            WebhookEndpoint.objects.filter(organization=organization, is_active=True)
        )
    except Exception:
        logger.exception("Webhook dispatch failed to load endpoints for event_type=%s", event_type)
        return

    for endpoint in endpoints:
        if event_type not in (endpoint.event_types or []):
            continue
        delivery_id = str(uuid.uuid4())
        try:
            deliver_webhook_task.delay(str(endpoint.id), event_type, delivery_id, payload)
        except Exception as exc:
            # Broker outage / serialization error. The business action already
            # committed; without an outbox this delivery is lost, so make it loud.
            logger.error(
                "Webhook enqueue failed (delivery lost) endpoint=%s event=%s delivery_id=%s",
                endpoint.id, event_type, delivery_id, exc_info=True,
            )
            _record_enqueue_failure(endpoint, event_type, delivery_id, payload, exc)


def _record_enqueue_failure(endpoint, event_type, delivery_id, payload, exc):
    """Write a failed WebhookDelivery row for a delivery that never reached the queue.

    Delivery rows are normally written only by the worker, so an enqueue failure
    otherwise leaves no trace in the admin delivery log. Recording it here makes
    the lost delivery visible (and its payload available for replay). Best-effort:
    must never raise into the caller's request path.
    """
    from tickets.models import WebhookDelivery

    try:
        WebhookDelivery.objects.create(
            endpoint=endpoint,
            organization_id=endpoint.organization_id,
            event_type=event_type,
            delivery_id=delivery_id,
            payload=payload,
            attempt=1,
            success=False,
            error_message=f"Enqueue failed (not sent): {type(exc).__name__}: {exc}"[:1000],
        )
    except Exception:
        logger.exception(
            "Failed to record webhook enqueue failure endpoint=%s delivery_id=%s",
            endpoint.id, delivery_id,
        )


# --- Convenience triggers -------------------------------------------------
# Each builds the payload eagerly (the instance is in memory at the call site)
# and defers the dispatch until the surrounding transaction commits. on_commit
# runs the callback immediately when no atomic block is active, so these are
# correct whether or not the caller is inside a transaction, and a rolled-back
# write never fires a webhook.

def fire_event_created(event):
    payload = build_event_payload(event)
    organization = event.organization
    transaction.on_commit(lambda: dispatch(EVENT_CREATED, organization, payload))


def fire_order_created(order):
    payload = build_order_payload(order)
    organization = order.event.organization
    transaction.on_commit(lambda: dispatch(ORDER_CREATED, organization, payload))


def fire_customer_created(customer):
    payload = build_customer_payload(customer)
    organization = customer.organization
    transaction.on_commit(lambda: dispatch(CUSTOMER_CREATED, organization, payload))
