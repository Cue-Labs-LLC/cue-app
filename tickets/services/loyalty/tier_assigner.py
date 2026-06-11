"""Loyalty tier assignment.

Evaluates every customer in an organization against a ``LoyaltyProgram``'s
tiers and assigns each customer to the best tier they qualify for. Mirrors the
RFM streaming design in ``tickets/services/segmentation/rfm_calculator.py``:
annotate the metrics each rule needs, stream customers in chunks, and
``bulk_update`` the denormalized ``Customer.loyalty_tier`` FK.

Per-customer count metrics (order_count, events_attended, tickets_purchased)
each use an isolated ``Subquery`` wrapped in ``Coalesce(..., 0)`` rather than
joined ``Count`` aggregates, so the joins for one metric never inflate the
rows counted for another (see CLAUDE.md "isolated Subqueries" rule).
"""
from django.db.models import Count, IntegerField, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from tickets.models import Customer, Ticket, TicketOrder

CHUNK_SIZE = 2000
UPDATE_FIELDS = ['loyalty_tier', 'loyalty_tier_updated_at']


def _count_subquery(queryset, group_field, distinct_field=None):
    """Build a grouped per-customer COUNT subquery returning a single column."""
    count_kwargs = {'distinct': True} if distinct_field else {}
    field = distinct_field or 'id'
    return Coalesce(
        Subquery(
            queryset.values(group_field)
            .annotate(c=Count(field, **count_kwargs))
            .values('c'),
            output_field=IntegerField(),
        ),
        0,
    )


class LoyaltyTierAssigner:
    """Assign customers to the tiers of a single ``LoyaltyProgram``."""

    def __init__(self, program):
        self.program = program
        self.organization = program.organization

    def _base_queryset(self):
        # Count rules ignore refunded and soft-deleted orders so they stay
        # consistent with Customer.lifetime_value (which excludes refunds).
        live_orders = TicketOrder.objects.filter(
            customer=OuterRef('pk'), refunded_at__isnull=True, deleted_at__isnull=True,
        )
        order_count = _count_subquery(live_orders, 'customer')
        events_purchased = _count_subquery(live_orders, 'customer', distinct_field='event')
        tickets_purchased = _count_subquery(
            Ticket.objects.filter(
                ticket_order__customer=OuterRef('pk'),
                ticket_order__refunded_at__isnull=True,
                ticket_order__deleted_at__isnull=True,
            ),
            'ticket_order__customer',
        )
        return (
            Customer.objects.filter(organization=self.organization)
            .exclude(email__endswith='@placeholder.local')
            .annotate(
                order_count=order_count,
                events_purchased=events_purchased,
                tickets_purchased=tickets_purchased,
            )
            .values(
                'id', 'lifetime_value', 'last_order_date', 'lifetime_points',
                'order_count', 'events_purchased', 'tickets_purchased',
            )
        )

    def calculate_all(self):
        """Assign every (non-placeholder) customer to their best tier.

        Returns the number of customers assigned to a tier (i.e. non-null).
        Customers who qualify for no tier are set to ``None`` so stale
        assignments from a previous program/run are cleared.
        """
        now = timezone.now().date()
        updated_at = timezone.now()
        # Best tier first: highest rank wins when several tiers qualify.
        tiers = list(self.program.tiers.all().order_by('-rank', 'name'))

        assigned = 0
        chunk = []
        for row in self._base_queryset().iterator(chunk_size=CHUNK_SIZE):
            last_order_date = row['last_order_date']
            days_since = (now - last_order_date).days if last_order_date else None
            tier_id = None
            for tier in tiers:
                if tier.qualifies(
                    lifetime_value=row['lifetime_value'],
                    order_count=row['order_count'],
                    events_purchased=row['events_purchased'],
                    tickets_purchased=row['tickets_purchased'],
                    days_since_last_order=days_since,
                    lifetime_points=row['lifetime_points'],
                ):
                    tier_id = tier.id
                    break
            if tier_id is not None:
                assigned += 1
            chunk.append(Customer(
                id=row['id'],
                loyalty_tier_id=tier_id,
                loyalty_tier_updated_at=updated_at,
            ))
            if len(chunk) >= CHUNK_SIZE:
                Customer.objects.bulk_update(chunk, UPDATE_FIELDS)
                chunk = []
        if chunk:
            Customer.objects.bulk_update(chunk, UPDATE_FIELDS)
        return assigned
