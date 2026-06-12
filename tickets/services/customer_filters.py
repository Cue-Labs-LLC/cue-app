"""Shared customer-filtering logic.

Single source of truth for translating a criteria dict into an org-scoped
``Customer`` queryset. Used by both the customer list view and the marketing-SMS
recipient-list resolver so the two never drift apart.

Criteria keys (all optional):
    search            str   — name/email icontains
    rfm_segment       str | list[str]
    tag_id            str (UUID)            — single tag (customer_list)
    tag_ids           list[str] (UUIDs)     — multiple tags (SMS lists)
    behavior_profile  str | list[str]
    min_ltv           Decimal | str | number
    last_order_after  date | 'YYYY-MM-DD'
    all_subscribers   bool — no-op narrowing; audience is the whole org. Used so an
                      "all opted-in subscribers" send has non-empty criteria (and so
                      passes SMSCampaign.candidate_customers' empty-criteria fail-safe)
                      without filtering anyone out here.
"""

import uuid as _uuid
from decimal import Decimal, InvalidOperation
from datetime import date, datetime

from tickets.models import Customer

# Customers imported from CSVs without a real email get a placeholder address;
# they are never a valid contact target for the UI or marketing.
PLACEHOLDER_EMAIL_SUFFIX = '@placeholder.local'


def _as_list(value):
    if value is None or value == '':
        return []
    if isinstance(value, (list, tuple, set)):
        return [v for v in value if v not in (None, '')]
    return [value]


def _valid_uuids(values):
    out = []
    for v in values:
        try:
            _uuid.UUID(str(v))
            out.append(str(v))
        except (ValueError, AttributeError, TypeError):
            continue
    return out


def filter_customers(org, criteria):
    """Return an org-scoped ``Customer`` queryset matching ``criteria``.

    Applies only the filters present in ``criteria``; an empty dict returns all
    of the org's (non-placeholder) customers, so callers that must avoid the
    "everyone" case (e.g. SMS recipient lists) are responsible for their own
    fail-safe before calling this.
    """
    criteria = criteria or {}
    qs = Customer.objects.filter(organization=org).exclude(
        email__endswith=PLACEHOLDER_EMAIL_SUFFIX
    )

    search = (criteria.get('search') or '').strip()
    if search:
        qs = qs.filter(name__icontains=search) | qs.filter(email__icontains=search)

    segments = _as_list(criteria.get('rfm_segment'))
    if segments:
        qs = qs.filter(rfm_segment__in=segments)

    profiles = _as_list(criteria.get('behavior_profile'))
    if profiles:
        qs = qs.filter(behavior_profile__in=profiles)

    tag_ids = _valid_uuids(_as_list(criteria.get('tag_ids')) + _as_list(criteria.get('tag_id')))
    if tag_ids:
        qs = qs.filter(tags__id__in=tag_ids)

    # Customers who bought a ticket to a specific event (used for event-scoped SMS
    # audiences). May join multiple orders → callers dedupe (candidate_customers
    # calls .distinct(); materialize() also dedupes by phone).
    event_ids = _valid_uuids(_as_list(criteria.get('event_id')) + _as_list(criteria.get('event_ids')))
    if event_ids:
        qs = qs.filter(ticket_orders__event_id__in=event_ids)

    # 'all_subscribers' is deliberately not narrowed here — it means "the whole org".
    # The opted-in + has-phone restriction lives in SMSCampaign.candidate_customers.



    min_ltv = criteria.get('min_ltv')
    if min_ltv not in (None, ''):
        try:
            qs = qs.filter(lifetime_value__gte=Decimal(str(min_ltv)))
        except (InvalidOperation, ValueError):
            pass

    last_order_after = criteria.get('last_order_after')
    if last_order_after:
        parsed = last_order_after
        if isinstance(parsed, datetime):
            parsed = parsed.date()
        elif isinstance(parsed, str):
            try:
                parsed = datetime.strptime(parsed, '%Y-%m-%d').date()
            except ValueError:
                parsed = None
        if isinstance(parsed, date):
            qs = qs.filter(last_order_date__gte=parsed)

    return qs
