"""Shared customer-filtering logic.

Single source of truth for translating a criteria dict into an org-scoped
``Customer`` queryset. Used by both the customer list view and the marketing-SMS
recipient-list resolver so the two never drift apart.

Criteria keys (all optional):
    search            str   — name/email icontains
    rfm_segment       str | list[str]
    tag_id            str (UUID)            — single tag (customer_list)
    tag_ids           list[str] (UUIDs)     — multiple tags (SMS lists)
    market_id         str (UUID) | '__none__'
    market_ids        list[str] (UUIDs and/or '__none__')
    behavior_profile  str | list[str]
    min_ltv           Decimal | str | number
    last_order_after  date | 'YYYY-MM-DD'
    last_order_before date | 'YYYY-MM-DD'
    all_subscribers   bool — no-op narrowing; audience is the whole org. Used so an
                      "all opted-in subscribers" send has non-empty criteria (and so
                      passes SMSCampaign.candidate_customers' empty-criteria fail-safe)
                      without filtering anyone out here.

Filter semantics:
    market_id + event_id: order-level AND — the SAME TicketOrder must be in the
    market AND for the event. A customer with order A (in market) and order B (for
    event) does NOT match unless a single order satisfies both.
"""

import uuid as _uuid
from decimal import Decimal, InvalidOperation
from datetime import date, datetime

from django.db.models import Q, Exists, OuterRef

from tickets.models import Customer, TicketOrder, Market

# Customers imported from CSVs without a real email get a placeholder address;
# they are never a valid contact target for the UI or marketing.
PLACEHOLDER_EMAIL_SUFFIX = '@placeholder.local'
NO_MARKET_VALUE = '__none__'


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


def _coerce_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value, '%Y-%m-%d').date()
        except ValueError:
            return None
    return None


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

    raw_market_ids = _as_list(criteria.get('market_ids')) + _as_list(criteria.get('market_id'))
    include_no_market = any(str(v) == NO_MARKET_VALUE for v in raw_market_ids)
    market_ids = _valid_uuids(v for v in raw_market_ids if str(v) != NO_MARKET_VALUE)
    market_active = bool(market_ids or include_no_market)

    # Customers who bought a ticket to a specific event (used for event-scoped SMS
    # audiences). May join multiple orders → callers dedupe (candidate_customers
    # calls .distinct(); materialize() also dedupes by phone).
    event_ids = _valid_uuids(_as_list(criteria.get('event_id')) + _as_list(criteria.get('event_ids')))
    event_active = bool(event_ids)

    if market_active and event_active:
        # Order-level AND: the SAME TicketOrder must be in the market AND for the event.
        # Uses Exists so both constraints are evaluated on a single order row.
        order_market_q = Q()
        if market_ids:
            order_market_q |= Q(event__market_id__in=market_ids)
        if include_no_market:
            order_market_q |= Q(event__market__isnull=True)
        qs = qs.filter(
            Exists(
                TicketOrder.objects.filter(
                    order_market_q,
                    customer=OuterRef('pk'),
                    event__organization=org,
                    event_id__in=event_ids,
                )
            )
        )
    elif market_active:
        market_q = Q()
        if market_ids:
            market_q |= Q(ticket_orders__event__organization=org, ticket_orders__event__market_id__in=market_ids)
        if include_no_market:
            market_q |= Q(ticket_orders__event__organization=org, ticket_orders__event__market__isnull=True)
        qs = qs.filter(market_q)
    elif event_active:
        qs = qs.filter(ticket_orders__event_id__in=event_ids)

    # 'all_subscribers' is deliberately not narrowed here — it means "the whole org".
    # The opted-in + has-phone restriction lives in SMSCampaign.candidate_customers.



    phone = (criteria.get('phone') or '').strip()
    if phone:
        qs = qs.filter(phone__icontains=phone)

    sms_opt_in = criteria.get('sms_opt_in')
    if sms_opt_in is not None:
        qs = qs.filter(sms_opt_in=sms_opt_in)

    min_ltv = criteria.get('min_ltv')
    if min_ltv not in (None, ''):
        try:
            qs = qs.filter(lifetime_value__gte=Decimal(str(min_ltv)))
        except (InvalidOperation, ValueError):
            pass

    max_ltv = criteria.get('max_ltv')
    if max_ltv not in (None, ''):
        try:
            qs = qs.filter(lifetime_value__lte=Decimal(str(max_ltv)))
        except (InvalidOperation, ValueError):
            pass

    last_order_after = _coerce_date(criteria.get('last_order_after'))
    if last_order_after:
        qs = qs.filter(last_order_date__gte=last_order_after)

    last_order_before = _coerce_date(criteria.get('last_order_before'))
    if last_order_before:
        qs = qs.filter(last_order_date__lte=last_order_before)

    return qs


def market_filter_options(org):
    """Return (markets, has_no_market) for market dropdown population.

    markets: list of Market objects ordered by name (empty list if org has none)
    has_no_market: True if any TicketOrder exists on an event with no market assigned
    """
    markets = list(Market.objects.filter(organization=org).order_by('name'))
    if not markets:
        return markets, False
    has_no_market = TicketOrder.objects.filter(
        customer__organization=org,
        event__organization=org,
        event__market__isnull=True,
    ).exists()
    return markets, has_no_market
