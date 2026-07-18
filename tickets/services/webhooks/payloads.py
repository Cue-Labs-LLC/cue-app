"""Payload builders for outbound webhook events.

Each builder takes a single ORM instance and returns a plain JSON-serializable
dict. UUIDs are emitted as strings, decimals as strings (never floats — money),
and dates/times as ISO 8601. Builders assume the related objects they touch
(event.venue, order.event, order.customer) are already loaded at the call site,
so they add no queries on the request path.
"""


def build_event_payload(event) -> dict:
    venue = event.venue
    return {
        'id': str(event.id),
        'name': event.name,
        'summary': event.summary or '',
        'ticketing_type': event.ticketing_type,
        'status': event.status,
        'start_date': event.start_date.isoformat() if event.start_date else None,
        'start_time': event.start_time.isoformat() if event.start_time else None,
        'end_date': event.end_date.isoformat() if event.end_date else None,
        'end_time': event.end_time.isoformat() if event.end_time else None,
        'timezone': event.timezone,
        'capacity': event.capacity,
        'venue': {
            'name': venue.name,
            'city': venue.city or None,
            'state': venue.state or None,
        } if venue else None,
        'created_at': event.created_at.isoformat(),
    }


def build_order_payload(order) -> dict:
    return {
        'id': str(order.id),
        'order_number': order.display_order_number,
        'event': {
            'id': str(order.event_id),
            'name': order.event.name,
        },
        'customer': {
            'id': str(order.customer_id),
            'email': order.customer.email or None,
            'name': order.customer.name or None,
        },
        'total_amount': str(order.total_amount),
        'is_in_person': order.is_in_person,
        'order_date': order.order_date.isoformat() if order.order_date else None,
        'created_at': order.created_at.isoformat(),
    }


def build_customer_payload(customer) -> dict:
    return {
        'id': str(customer.id),
        'email': customer.email or None,
        'name': customer.name or None,
        'phone': customer.phone or None,
        'sms_opt_in': customer.sms_opt_in,
        'created_at': customer.created_at.isoformat(),
    }
