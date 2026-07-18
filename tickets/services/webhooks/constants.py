"""Canonical outbound-webhook event types.

To add a new event: append a constant here, add it to WEBHOOK_EVENT_TYPES,
write a payload builder in `payloads.py`, and add one `dispatch(...)` call at
the creation site.
"""

EVENT_CREATED = 'event.created'
ORDER_CREATED = 'order.created'
CUSTOMER_CREATED = 'customer.created'

WEBHOOK_EVENT_TYPES = [
    EVENT_CREATED,
    ORDER_CREATED,
    CUSTOMER_CREATED,
]

WEBHOOK_EVENT_TYPE_CHOICES = [(t, t) for t in WEBHOOK_EVENT_TYPES]
