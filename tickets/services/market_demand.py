"""
Recent ticket demand per market: average tickets sold per event.

For a chosen window of event dates, every non-cancelled event already held
(start_date <= today) counts as one event in its market. Each event's tickets
are counted with an isolated Subquery, so there is no join inflation. Events
that sold nothing stay in the denominator. The same numbers are computed for
the immediately preceding window of equal length, so each market shows how
demand moved.

The ticket filter (all / paid / free) narrows which tickets are counted, by
per-ticket price ($0 = free). It never changes which events count, so "avg paid
tickets per event" stays comparable across markets: a free-only show counts as
0 paid tickets rather than dropping out.

In-person orders and refunded orders are counted, matching the tickets metric
on Market Trends.
"""
from datetime import date, timedelta

from django.db.models import Count, IntegerField, OuterRef, Subquery
from django.db.models.functions import Coalesce

from tickets.models import EVENT_STATUS_CANCELLED, Event, Ticket
from tickets.services.conversion import _stats
from tickets.services.markets import NO_MARKET_LABEL


TICKET_FILTER_ALL = 'all'
TICKET_FILTER_PAID = 'paid'
TICKET_FILTER_FREE = 'free'
VALID_TICKET_FILTERS = (TICKET_FILTER_ALL, TICKET_FILTER_PAID, TICKET_FILTER_FREE)


def normalize_ticket_filter(value):
    """Return a valid ticket filter key, falling back to 'all'."""
    return value if value in VALID_TICKET_FILTERS else TICKET_FILTER_ALL


def _pct_change(current, prior):
    """Percent change from prior to current, or None when there is no usable prior."""
    if current is None or not prior:
        return None
    return round((current - prior) / prior * 100, 1)


class MarketDemandCalculator:
    def __init__(self, organization, start_date, end_date, ticket_filter=TICKET_FILTER_ALL):
        self.organization = organization
        self.ticket_filter = normalize_ticket_filter(ticket_filter)
        today = date.today()
        # Upcoming events haven't finished selling; clamp the window to today.
        self.start_date = start_date
        self.end_date = min(end_date, today)
        # Prior window: same length, ending the day before this window starts.
        length = (self.end_date - self.start_date).days + 1
        self.prior_end = self.start_date - timedelta(days=1)
        self.prior_start = self.prior_end - timedelta(days=max(length, 1) - 1)

    def _ticket_queryset(self):
        tickets = Ticket.objects.filter(ticket_order__event=OuterRef('pk'))
        if self.ticket_filter == TICKET_FILTER_FREE:
            tickets = tickets.filter(price=0)
        elif self.ticket_filter == TICKET_FILTER_PAID:
            tickets = tickets.filter(price__gt=0)
        return tickets

    def _event_rows(self, start, end):
        """One row per held, non-cancelled event in [start, end] with its ticket count."""
        if start > end:
            return []
        sold_subq = Subquery(
            self._ticket_queryset()
            .order_by()
            .values('ticket_order__event')
            .annotate(c=Count('id'))
            .values('c')[:1],
            output_field=IntegerField(),
        )
        return list(
            Event.objects.filter(
                organization=self.organization,
                start_date__gte=start,
                start_date__lte=end,
            )
            .exclude(status=EVENT_STATUS_CANCELLED)
            .annotate(sold=Coalesce(sold_subq, 0))
            .values('id', 'name', 'start_date', 'market_id', 'market__name', 'sold')
        )

    @staticmethod
    def _group(rows):
        """Group per-event rows by market id -> {'label', 'sold': [..], 'rows': [..]}."""
        groups = {}
        for row in rows:
            key = row['market_id']
            label = (row['market__name'] or '').strip() or NO_MARKET_LABEL
            g = groups.setdefault(key, {'label': label, 'sold': [], 'rows': []})
            g['sold'].append(row['sold'])
            g['rows'].append(row)
        return groups

    def calculate(self):
        current_rows = self._event_rows(self.start_date, self.end_date)
        prior_rows = self._event_rows(self.prior_start, self.prior_end)
        current = self._group(current_rows)
        prior = self._group(prior_rows)

        markets = []
        for key, g in current.items():
            stats = _stats(g['sold'], 1)
            top = max(g['rows'], key=lambda r: (r['sold'], r['start_date']))
            prior_stats = _stats(prior[key]['sold'], 1) if key in prior else None
            prior_avg = prior_stats['avg'] if prior_stats else None
            markets.append({
                'key': str(key) if key else 'none',
                'label': g['label'],
                'event_count': len(g['sold']),
                'tickets_sold': sum(g['sold']),
                'avg_per_event': stats['avg'],
                'median_per_event': stats['median'],
                'prior_avg': prior_avg,
                'prior_event_count': len(prior[key]['sold']) if key in prior else 0,
                'change_pct': _pct_change(stats['avg'], prior_avg),
                'top_event': {
                    'id': str(top['id']),
                    'name': top['name'],
                    'sold': top['sold'],
                },
            })
        markets.sort(key=lambda m: (-m['avg_per_event'], m['label']))

        all_sold = [r['sold'] for r in current_rows]
        prior_sold = [r['sold'] for r in prior_rows]
        overall_avg = _stats(all_sold, 1)['avg']
        prior_overall_avg = _stats(prior_sold, 1)['avg']
        summary = {
            'avg_per_event': overall_avg,
            'prior_avg': prior_overall_avg,
            'change_pct': _pct_change(overall_avg, prior_overall_avg),
            'event_count': len(all_sold),
            'tickets_sold': sum(all_sold),
            'market_count': len(markets),
        }
        return {
            'markets': markets,
            'summary': summary,
            'start_date': self.start_date,
            'end_date': self.end_date,
            'prior_start': self.prior_start,
            'prior_end': self.prior_end,
        }
