"""Loyalty tier assignment.

Evaluates every customer in an organization against a ``LoyaltyProgram``'s
tiers and assigns each customer to the best tier they qualify for. Mirrors the
RFM streaming design in ``tickets/services/segmentation/rfm_calculator.py``:
annotate the metrics each rule needs, stream customers in chunks, and
``bulk_update`` the denormalized ``Customer.loyalty_tier`` FK.

Per-customer count metrics (order_count, events_purchased, tickets_purchased,
events_attended) each use an isolated ``Subquery`` wrapped in ``Coalesce(..., 0)``
rather than joined ``Count`` aggregates, so the joins for one metric never inflate
the rows counted for another (see CLAUDE.md "isolated Subqueries" rule).

Note the *purchased* metrics count every live order, whereas *attended* metrics
count only tickets that were scanned in at the door (``Ticket.scanned_at``), so a
free-RSVP no-show raises events_purchased but not events_attended.

Tiers may window the attendance count with ``attended_within_days`` ("attended N
distinct events within the last D days"). We add one extra distinct-events
Subquery per distinct window value present across the program's tiers, filtered
to ``scanned_at >= now - D days``, and hand each tier the count for its own window.
"""
from datetime import timedelta

from django.db.models import Count, IntegerField, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from tickets.models import Customer, LoyaltyTierTransition, Ticket, TicketOrder

CHUNK_SIZE = 2000
UPDATE_FIELDS = ['loyalty_tier', 'loyalty_tier_updated_at']


def _window_key(days):
    """Annotation/row key for the distinct-events-attended count within ``days``."""
    return f'events_attended_w{days}'


def _paid_window_key(days):
    """Annotation/row key for the distinct-paid-events count within ``days``."""
    return f'paid_events_w{days}'


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

    def _base_queryset(self, windows, paid_windows):
        # Count rules ignore refunded and soft-deleted orders so they stay
        # consistent with Customer.lifetime_value (which excludes refunds).
        live_orders = TicketOrder.objects.filter(
            customer=OuterRef('pk'), refunded_at__isnull=True, deleted_at__isnull=True,
        )
        order_count = _count_subquery(live_orders, 'customer')
        # Paid orders: live orders where money was actually paid (total > $0),
        # so free-RSVP orders never count toward the "paid events" rule. The rule
        # counts distinct events, so several paid orders to one event count once.
        paid_orders = live_orders.filter(total_amount__gt=0)
        events_purchased = _count_subquery(live_orders, 'customer', distinct_field='event')
        tickets_purchased = _count_subquery(
            Ticket.objects.filter(
                ticket_order__customer=OuterRef('pk'),
                ticket_order__refunded_at__isnull=True,
                ticket_order__deleted_at__isnull=True,
            ),
            'ticket_order__customer',
        )
        # Attendance metrics: only tickets actually scanned in at the door.
        attended_tickets = Ticket.objects.filter(
            ticket_order__customer=OuterRef('pk'),
            ticket_order__refunded_at__isnull=True,
            ticket_order__deleted_at__isnull=True,
            scanned_at__isnull=False,
        )
        annotations = {
            'order_count': order_count,
            'events_purchased': events_purchased,
            'tickets_purchased': tickets_purchased,
            'events_attended': _count_subquery(
                attended_tickets, 'ticket_order__customer', distinct_field='ticket_order__event',
            ),
            'paid_event_count': _count_subquery(paid_orders, 'customer', distinct_field='event'),
        }
        # One windowed distinct-events count per distinct attendance window in use.
        now = timezone.now()
        for days in windows:
            cutoff = now - timedelta(days=days)
            annotations[_window_key(days)] = _count_subquery(
                attended_tickets.filter(scanned_at__gte=cutoff),
                'ticket_order__customer', distinct_field='ticket_order__event',
            )
        # One windowed distinct-paid-events count per distinct paid-events window.
        for days in paid_windows:
            cutoff = now - timedelta(days=days)
            annotations[_paid_window_key(days)] = _count_subquery(
                paid_orders.filter(order_date__gte=cutoff), 'customer', distinct_field='event',
            )
        return (
            Customer.objects.filter(organization=self.organization)
            .exclude(email__endswith='@placeholder.local')
            .annotate(**annotations)
            .values(
                'id', 'lifetime_value', 'last_order_date', 'lifetime_points',
                'loyalty_tier',
                *annotations.keys(),
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
        windows = sorted({t.attended_within_days for t in tiers if t.attended_within_days})
        paid_windows = sorted({t.paid_events_within_days for t in tiers if t.paid_events_within_days})

        assigned = 0
        chunk = []
        # Record tier changes so the customer timeline can show progression.
        # Forward-only: we only capture changes observed against each customer's
        # currently-stored tier, appending one immutable LoyaltyTierTransition
        # per change and flushing on the same chunk cadence as the bulk_update.
        transitions = []
        for row in self._base_queryset(windows, paid_windows).iterator(chunk_size=CHUNK_SIZE):
            last_order_date = row['last_order_date']
            days_since = (now - last_order_date).days if last_order_date else None
            tier_id = None
            for tier in tiers:
                window = tier.attended_within_days
                in_window = row[_window_key(window)] if window else 0
                paid_window = tier.paid_events_within_days
                paid_in_window = row[_paid_window_key(paid_window)] if paid_window else 0
                if tier.qualifies(
                    lifetime_value=row['lifetime_value'],
                    order_count=row['order_count'],
                    events_purchased=row['events_purchased'],
                    tickets_purchased=row['tickets_purchased'],
                    days_since_last_order=days_since,
                    lifetime_points=row['lifetime_points'],
                    events_attended=row['events_attended'],
                    events_attended_in_window=in_window,
                    paid_event_count=row['paid_event_count'],
                    paid_event_count_in_window=paid_in_window,
                ):
                    tier_id = tier.id
                    break
            if tier_id is not None:
                assigned += 1
            old_tier_id = row['loyalty_tier']
            if old_tier_id != tier_id:
                transitions.append(LoyaltyTierTransition(
                    customer_id=row['id'],
                    organization=self.organization,
                    from_tier_id=old_tier_id,
                    to_tier_id=tier_id,
                    changed_at=updated_at,
                ))
            chunk.append(Customer(
                id=row['id'],
                loyalty_tier_id=tier_id,
                loyalty_tier_updated_at=updated_at,
            ))
            if len(chunk) >= CHUNK_SIZE:
                Customer.objects.bulk_update(chunk, UPDATE_FIELDS)
                chunk = []
            if len(transitions) >= CHUNK_SIZE:
                LoyaltyTierTransition.objects.bulk_create(transitions)
                transitions = []
        if chunk:
            Customer.objects.bulk_update(chunk, UPDATE_FIELDS)
        if transitions:
            LoyaltyTierTransition.objects.bulk_create(transitions)
        return assigned
