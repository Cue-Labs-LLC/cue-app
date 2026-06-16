"""
Event-scoped audience analytics: new vs returning buyers, who checked in
(new vs returning), first-time attendees, and high-value attendance.

Derives everything from existing TicketOrder / Ticket / Customer data:
- Check-ins use Ticket.scanned_at (the single source of truth, set by the live
  scanner and by POSH CSV imports).
- "Returning" reuses the same rule as the event Overview tab (views._compute_event_stats):
  a buyer is returning if they have an online order at a *different* event in the org.
- "High value" is the org-relative signal RFM already computes: VIP / Big Spender
  segment, or top spend tiers (rfm_monetary_score >= 4).
"""
from django.db.models import Count, IntegerField, Min, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce

from tickets.models import Customer, TicketOrder

# A customer counts as "high value" via either signal: a top RFM segment, or a
# top-spend monetary quintile (4-5 == top 40% of org spend).
HIGH_VALUE_Q = Q(rfm_segment__in=['VIP', 'Big Spender']) | Q(rfm_monetary_score__gte=4)


class EventAudienceCalculator:
    """Analytics for a single event's audience. Construct with the Event."""

    def __init__(self, event):
        self.event = event
        self.organization = event.organization

    def calculate(self):
        event = self.event
        org = self.organization

        # All online buyers for this event (exclude in-person placeholder orders).
        buyer_ids = set(
            TicketOrder.objects.filter(event=event, is_in_person=False)
            .values_list('customer_id', flat=True)
            .distinct()
        )
        total_buyers = len(buyer_ids)
        if not buyer_ids:
            return self._empty()

        # Returning = has an online order at a *different* event in this org.
        returning_ids = set(
            TicketOrder.objects.filter(
                customer_id__in=buyer_ids,
                customer__organization=org,
                is_in_person=False,
            )
            .exclude(event=event)
            .values_list('customer_id', flat=True)
            .distinct()
        )
        returning_buyers = len(returning_ids)
        new_buyers = total_buyers - returning_buyers

        # Attendees = buyers with at least one scanned ticket at this event.
        attendee_ids = set(
            TicketOrder.objects.filter(
                event=event,
                is_in_person=False,
                tickets__scanned_at__isnull=False,
            )
            .values_list('customer_id', flat=True)
            .distinct()
        )
        attendees = len(attendee_ids)
        attendees_returning = len(attendee_ids & returning_ids)
        attendees_new = attendees - attendees_returning
        # First-time attendees: new to the org (first-ever purchase) AND they showed up.
        first_time_attendees = attendees_new

        # High-value buyers, and how many of them actually attended.
        high_value_buyer_ids = set(
            Customer.objects.filter(HIGH_VALUE_Q, id__in=buyer_ids)
            .values_list('id', flat=True)
        )
        high_value_total = len(high_value_buyer_ids)
        high_value_attended_ids = high_value_buyer_ids & attendee_ids
        high_value_attended = len(high_value_attended_ids)

        return {
            'show': True,
            'total_buyers': total_buyers,
            'new_buyers': new_buyers,
            'returning_buyers': returning_buyers,
            'attendees': attendees,
            'attendees_new': attendees_new,
            'attendees_returning': attendees_returning,
            'first_time_attendees': first_time_attendees,
            'high_value_attendees': high_value_attended,
            'high_value_total': high_value_total,
            'high_value_attended': high_value_attended,
        }

    def notable_attendees_queryset(self):
        """Customers worth highlighting among this event's attendees.

        "Notable" = high-value (VIP / Big Spender / top spend) OR a frequent buyer
        (purchased at 2+ distinct events). Annotated with their total distinct events
        purchased and their earliest scan at this event, ranked so the most frequent
        repeat buyers surface first. Returned as a queryset for the view to paginate.
        """
        event = self.event
        org = self.organization

        attendee_ids = set(
            TicketOrder.objects.filter(
                event=event,
                is_in_person=False,
                tickets__scanned_at__isnull=False,
            )
            .values_list('customer_id', flat=True)
            .distinct()
        )
        if not attendee_ids:
            return Customer.objects.none()

        # Distinct events this customer has purchased at (org-scoped, online orders).
        # Isolated Subquery to avoid join inflation against the scan annotation below.
        events_count_subq = (
            TicketOrder.objects.filter(
                customer_id=OuterRef('pk'),
                customer__organization=org,
                is_in_person=False,
            )
            .order_by()
            .values('customer_id')
            .annotate(c=Count('event_id', distinct=True))
            .values('c')
        )

        return (
            Customer.objects.filter(id__in=attendee_ids)
            .annotate(
                events_count=Coalesce(
                    Subquery(events_count_subq, output_field=IntegerField()), 0
                ),
                event_checked_in_at=Min(
                    'ticket_orders__tickets__scanned_at',
                    filter=Q(ticket_orders__event=event),
                ),
            )
            .filter(HIGH_VALUE_Q | Q(events_count__gte=2))
            .order_by('-events_count', '-lifetime_value')
        )

    def _empty(self):
        return {
            'show': True,
            'total_buyers': 0,
            'new_buyers': 0,
            'returning_buyers': 0,
            'attendees': 0,
            'attendees_new': 0,
            'attendees_returning': 0,
            'first_time_attendees': 0,
            'high_value_attendees': 0,
            'high_value_total': 0,
            'high_value_attended': 0,
        }
