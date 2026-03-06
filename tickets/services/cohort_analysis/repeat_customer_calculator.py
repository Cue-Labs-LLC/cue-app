"""
Classifies event attendees as new (first-ever purchase) or returning.
Derives all data from existing TicketOrder + Event tables.
"""
import pandas as pd
from tickets.models import TicketOrder, Event


class RepeatCustomerCalculator:
    def __init__(self, organization):
        self.organization = organization

    def calculate(self):
        orders_qs = TicketOrder.objects.filter(
            customer__organization=self.organization,
            is_in_person=False,
        ).values('customer_id', 'event_id', 'order_date')

        rows = list(orders_qs)
        if not rows:
            return {'events': [], 'summary': {'total_attendees': 0, 'new_count': 0, 'returning_count': 0, 'new_pct': 0, 'returning_pct': 0}}

        df = pd.DataFrame(rows)

        # Each customer's first order date determines their "new" event
        first_orders = df.groupby('customer_id')['order_date'].min().reset_index()
        first_orders.columns = ['customer_id', 'first_order_date']

        # Merge to find which event(s) each customer's first order belongs to
        df = df.merge(first_orders, on='customer_id')
        df['is_new'] = df['order_date'] == df['first_order_date']

        # Deduplicate: one row per customer per event
        customer_events = df.drop_duplicates(subset=['customer_id', 'event_id'])

        # For customers who appear "new" at multiple events (same order_date), only
        # mark them new at the first event_id alphabetically to avoid double-counting
        new_flags = customer_events.groupby('customer_id')['is_new'].transform('sum')
        # If a customer has is_new=True for multiple events, keep only the first
        customer_events = customer_events.copy()
        customer_events['_new_rank'] = customer_events.groupby('customer_id').cumcount()
        customer_events.loc[
            (customer_events['is_new']) & (customer_events['_new_rank'] > 0),
            'is_new'
        ] = False

        # Aggregate per event
        event_stats = customer_events.groupby('event_id').agg(
            total=('customer_id', 'count'),
            new_count=('is_new', 'sum'),
        ).reset_index()
        event_stats['returning_count'] = event_stats['total'] - event_stats['new_count']
        event_stats['returning_pct'] = (
            (event_stats['returning_count'] / event_stats['total'] * 100)
            .round(1)
        )

        # Fetch event details
        event_ids = event_stats['event_id'].tolist()
        events_qs = Event.objects.filter(id__in=event_ids).select_related('venue')
        event_map = {e.id: e for e in events_qs}

        events_list = []
        for _, row in event_stats.iterrows():
            event = event_map.get(row['event_id'])
            if not event:
                continue
            events_list.append({
                'event_id': str(event.id),
                'event_name': event.name,
                'event_date': event.start_date.isoformat(),
                'venue_name': event.venue.name if event.venue else '',
                'venue_city': event.venue.city if event.venue else '',
                'total': int(row['total']),
                'new_count': int(row['new_count']),
                'returning_count': int(row['returning_count']),
                'returning_pct': float(row['returning_pct']),
            })

        # Sort chronologically
        events_list.sort(key=lambda x: x['event_date'])

        total_attendees = sum(e['total'] for e in events_list)
        total_new = sum(e['new_count'] for e in events_list)
        total_returning = sum(e['returning_count'] for e in events_list)

        summary = {
            'total_attendees': total_attendees,
            'new_count': total_new,
            'returning_count': total_returning,
            'new_pct': round(total_new / total_attendees * 100, 1) if total_attendees else 0,
            'returning_pct': round(total_returning / total_attendees * 100, 1) if total_attendees else 0,
        }

        return {'events': events_list, 'summary': summary}
