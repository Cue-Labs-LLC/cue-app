"""General-purpose outbound webhook system.

Public entrypoint: `dispatch(event_type, organization, payload)` enqueues a
signed HTTP POST to every active WebhookEndpoint in the org subscribed to
`event_type`. Payload builders live in `payloads`, HMAC signing in `signing`,
and the canonical event-type constants in `constants`.
"""

from .constants import (
    EVENT_CREATED,
    ORDER_CREATED,
    CUSTOMER_CREATED,
    WEBHOOK_EVENT_TYPES,
    WEBHOOK_EVENT_TYPE_CHOICES,
)
from .dispatch import (
    dispatch,
    fire_event_created,
    fire_order_created,
    fire_customer_created,
)
from .payloads import (
    build_event_payload,
    build_order_payload,
    build_customer_payload,
)

__all__ = [
    'EVENT_CREATED',
    'ORDER_CREATED',
    'CUSTOMER_CREATED',
    'WEBHOOK_EVENT_TYPES',
    'WEBHOOK_EVENT_TYPE_CHOICES',
    'dispatch',
    'fire_event_created',
    'fire_order_created',
    'fire_customer_created',
    'build_event_payload',
    'build_order_payload',
    'build_customer_payload',
]
