"""Shared bulk-tagging logic.

Single home for "apply a tag to a set of customers", used by the churn page, the
customer list, and the event Customers tab so the three never drift apart. The
tag is either an existing one (by UUID) or created on demand by name.
"""

import uuid as _uuid


def resolve_tag(org, *, tag_id=None, new_tag_name=None):
    """Return a CustomerTag for `org`: an existing one by UUID, or get_or_create
    by name. Returns None if neither a valid tag_id nor a non-empty name is given."""
    from tickets.models import CustomerTag

    name = (new_tag_name or '').strip()
    if name:
        tag, _ = CustomerTag.objects.get_or_create(
            organization=org, name=name[:50], defaults={'color': 'blue'},
        )
        return tag

    tag_id = (tag_id or '').strip()
    if not tag_id:
        return None
    try:
        _uuid.UUID(tag_id)
    except ValueError:
        return None
    return CustomerTag.objects.filter(organization=org, id=tag_id).first()


def tag_customers(org, customers_qs, *, tag_id=None, new_tag_name=None):
    """Apply a tag to every customer in `customers_qs` (already org-scoped).

    Returns (tag, count). tag is None when no valid/creatable tag was given —
    callers should treat that as an error and not report success."""
    tag = resolve_tag(org, tag_id=tag_id, new_tag_name=new_tag_name)
    if tag is None:
        return None, 0
    count = 0
    for customer in customers_qs:
        customer.tags.add(tag)
        count += 1
    return tag, count
