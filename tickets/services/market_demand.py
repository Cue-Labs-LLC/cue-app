"""
Recent ticket demand per market.

For a chosen window of event dates, every non-cancelled event already held
(start_date <= today) counts as one event in its market. Each event's tickets
are counted with an isolated Subquery, so there is no join inflation. Events
that sold nothing stay in the denominator. The same numbers are computed for
the immediately preceding window of equal length, so each market shows how it
moved.

The ticket filter (all / paid / free) narrows which tickets are counted, by
per-ticket price ($0 = free). It never changes which events count.

In-person orders and refunded orders are counted in tickets sold, matching the
tickets metric on Market Trends.

Ad spend is each event's marketing expenses (manual + Meta Ads lines, both
category='marketing'). It is not affected by the ticket filter.

Returning audience vs. new buyers
---------------------------------
Measuring what ads *caused* isn't possible from this data: budgets follow
sales (slow shows get pushed harder), and Meta's attribution misses a large,
varying share of ad-driven sales. So instead of estimating "sales without
ads", the page reports two first-party measures:

  * Returning audience -- tickets bought by people who had already bought at an
    earlier event in the same market. The closest proxy for organic demand: a
    built-up local base. (Some of these buyers may have clicked an ad; they
    still came because of a past show.)
  * New buyers per $1k of ad spend -- tickets bought by people new to that
    market, per $1,000 of the market's ad spend. Ad efficiency: the same sales
    on less spend scores higher, and new cities (which run on ads) are judged
    on this rather than penalized for having no audience yet.

"New" means new to *that market*. In-person orders carry no real buyer
identity, so they are left out of the returning/new split (but still count in
tickets sold).

Fill rate
---------
Per event: *paid* tickets sold / capacity (the event's own capacity, else its
venue's). Always paid, whatever the ticket filter: free RSVPs are overbooked on
purpose and mostly don't show (on production ~20% of free tickets are scanned
vs. ~77% of paid), so counting them reads as 130%+ "full" rooms. A market's
fill rate is the average of its events' fill rates; events with no capacity are
left out. Not capped at 100% -- an oversold show reads above 100%.

Fill rate one week out is the same ratio using only paid tickets ordered on or
before the date 7 days before the event (order date in the site timezone): how
full the room was a week ahead -- an early-demand signal next to the final.
"""
from datetime import date, timedelta

from django.db.models import (
    Count, DateField, Exists, ExpressionWrapper, F, IntegerField, Min, OuterRef, Q, Subquery,
)
from django.db.models.functions import Coalesce

from tickets.models import EVENT_STATUS_CANCELLED, Event, Ticket, TicketOrder
from tickets.services.conversion import _marketing_spend_expr, _stats
from tickets.services.markets import NO_MARKET_LABEL


TICKET_FILTER_ALL = 'all'
TICKET_FILTER_PAID = 'paid'
TICKET_FILTER_FREE = 'free'
VALID_TICKET_FILTERS = (TICKET_FILTER_ALL, TICKET_FILTER_PAID, TICKET_FILTER_FREE)

RANK_TICKETS = 'tickets'
RANK_RETURNING = 'returning'
RANK_NEW_PER_1K = 'new_per_1k'
RANK_FILL_RATE = 'fill_rate'
# "Fill one week out": paid tickets ordered at least this many days before the event.
EARLY_FILL_DAYS = 7
VALID_RANKS = (RANK_TICKETS, RANK_RETURNING, RANK_NEW_PER_1K, RANK_FILL_RATE)


def normalize_ticket_filter(value):
    """Return a valid ticket filter key, falling back to 'all'."""
    return value if value in VALID_TICKET_FILTERS else TICKET_FILTER_ALL


def normalize_rank(value):
    """Return a valid rank key, falling back to tickets per event."""
    return value if value in VALID_RANKS else RANK_TICKETS


def _pct_change(current, prior):
    """Percent change from prior to current, or None when there is no usable prior."""
    if current is None or not prior:
        return None
    return round((current - prior) / prior * 100, 1)


def _ticket_filter_q(ticket_filter):
    if ticket_filter == TICKET_FILTER_FREE:
        return Q(price=0)
    if ticket_filter == TICKET_FILTER_PAID:
        return Q(price__gt=0)
    return Q()


def _group_metrics(rows):
    """Per-market numbers for a list of event rows from one window."""
    n = len(rows)
    sold = [r['sold'] for r in rows]
    stats = _stats(sold, 1)
    returning = sum(r['returning'] for r in rows)
    new = sum(r['new'] for r in rows)
    identified = returning + new
    spend = float(sum(r['ad_spend'] for r in rows))
    fills = [r['paid_sold'] / r['capacity'] * 100 for r in rows if r['capacity'] and r['capacity'] > 0]
    fill_rate = round(sum(fills) / len(fills), 1) if fills else None
    early = [r['paid_week_out'] / r['capacity'] * 100 for r in rows if r['capacity'] and r['capacity'] > 0]
    fill_week_out = round(sum(early) / len(early), 1) if early else None
    return {
        'event_count': n,
        'tickets_sold': sum(sold),
        'stats': stats,
        'returning': returning,
        'new': new,
        'spend': spend,
        'metrics': {
            RANK_TICKETS: stats['avg'],
            RANK_RETURNING: round(returning / n, 1) if n else None,
            # Undefined without spend: nothing to be efficient with.
            RANK_NEW_PER_1K: round(new / (spend / 1000), 1) if spend > 0 else None,
            RANK_FILL_RATE: fill_rate,
        },
        'capacity_events': len(fills),
        'fill_week_out': fill_week_out,
        'returning_share': round(returning / identified * 100, 1) if identified else None,
    }


class MarketDemandCalculator:
    def __init__(self, organization, start_date, end_date, ticket_filter=TICKET_FILTER_ALL,
                 rank=RANK_TICKETS):
        self.organization = organization
        self.ticket_filter = normalize_ticket_filter(ticket_filter)
        self.rank = normalize_rank(rank)
        today = date.today()
        # Upcoming events haven't finished selling; clamp the window to today.
        self.start_date = start_date
        self.end_date = min(end_date, today)
        # Prior window: same length, ending the day before this window starts.
        length = (self.end_date - self.start_date).days + 1
        self.prior_end = self.start_date - timedelta(days=1)
        self.prior_start = self.prior_end - timedelta(days=max(length, 1) - 1)

    def _tickets_subquery(self, ref_field, ticket_filter=None):
        """Count of tickets (under the page's ticket filter, unless one is given)
        for the row OuterRef('pk') points at."""
        return Subquery(
            Ticket.objects.filter(**{ref_field: OuterRef('pk')})
            .filter(_ticket_filter_q(ticket_filter or self.ticket_filter))
            .order_by()
            .values(ref_field)
            .annotate(c=Count('id'))
            .values('c')[:1],
            output_field=IntegerField(),
        )

    def _event_rows(self, start, end):
        """One row per held, non-cancelled event in [start, end] with its ticket count."""
        if start > end:
            return []
        rows = list(
            Event.objects.filter(
                organization=self.organization,
                start_date__gte=start,
                start_date__lte=end,
            )
            .exclude(status=EVENT_STATUS_CANCELLED)
            # Each stat is its own Subquery, so tickets and spend never inflate each other.
            .annotate(
                sold=Coalesce(self._tickets_subquery('ticket_order__event'), 0),
                ad_spend=_marketing_spend_expr(),
                # Fill rate is always paid tickets / capacity (event's, else venue's).
                paid_sold=Coalesce(self._tickets_subquery('ticket_order__event', TICKET_FILTER_PAID), 0),
                capacity_or_venue=Coalesce('capacity', 'venue__capacity'),
                week_out=ExpressionWrapper(
                    F('start_date') - timedelta(days=EARLY_FILL_DAYS), output_field=DateField(),
                ),
            )
            # Separate annotate so the subquery can reference week_out.
            .annotate(paid_week_out=Coalesce(self._paid_by_week_out_subquery(), 0))
            .values(
                'id', 'name', 'start_date', 'market_id', 'market__name', 'sold', 'ad_spend',
                'paid_sold', 'paid_week_out', 'capacity_or_venue',
            )
        )
        for r in rows:
            r['capacity'] = r.pop('capacity_or_venue')
        self._attach_buyer_split(rows)
        return rows

    @staticmethod
    def _paid_by_week_out_subquery():
        """Paid tickets for the outer event ordered on/before its week_out date."""
        return Subquery(
            Ticket.objects.filter(
                ticket_order__event=OuterRef('pk'),
                price__gt=0,
                ticket_order__order_date__date__lte=OuterRef('week_out'),
            )
            .order_by()
            .values('ticket_order__event')
            .annotate(c=Count('id'))
            .values('c')[:1],
            output_field=IntegerField(),
        )

    def _attach_buyer_split(self, rows):
        """Add 'returning' / 'new' ticket counts to each event row.

        A ticket is "returning" when its buyer had an order at an earlier
        (non-cancelled) event in the same market; otherwise "new". In-person
        orders are skipped. One query over the window's orders, with the
        returning check as an Exists subquery per order.
        """
        for r in rows:
            r['returning'] = 0
            r['new'] = 0
        if not rows:
            return
        earlier = TicketOrder.objects.filter(
            customer_id=OuterRef('customer_id'),
            is_in_person=False,
            event__organization=self.organization,
            event__start_date__lt=OuterRef('event__start_date'),
        ).exclude(event__status=EVENT_STATUS_CANCELLED)
        orders = (
            TicketOrder.objects.filter(
                event_id__in=[r['id'] for r in rows], is_in_person=False,
            )
            .annotate(
                ticket_count=Coalesce(self._tickets_subquery('ticket_order'), 0),
                # Same market as this order's event. Events with no market pool
                # together (NULL never equals NULL in SQL, hence the 2nd Exists).
                returning_market=Exists(earlier.filter(event__market_id=OuterRef('event__market_id'))),
                returning_unassigned=Exists(earlier.filter(event__market__isnull=True)),
            )
            .values('event_id', 'event__market_id', 'ticket_count', 'returning_market', 'returning_unassigned')
        )
        by_id = {r['id']: r for r in rows}
        for o in orders:
            if o['event__market_id'] is None:
                is_returning = o['returning_unassigned']
            else:
                is_returning = o['returning_market']
            by_id[o['event_id']]['returning' if is_returning else 'new'] += o['ticket_count']

    def _first_event_dates(self):
        """Each market's first-ever held, non-cancelled event date (for "New city")."""
        return dict(
            Event.objects.filter(organization=self.organization, start_date__lte=self.end_date)
            .exclude(status=EVENT_STATUS_CANCELLED)
            .values('market_id')
            .annotate(first=Min('start_date'))
            .values_list('market_id', 'first')
        )

    @staticmethod
    def _group(rows):
        groups = {}
        for row in rows:
            key = row['market_id']
            label = (row['market__name'] or '').strip() or NO_MARKET_LABEL
            groups.setdefault(key, {'label': label, 'rows': []})['rows'].append(row)
        return groups

    def calculate(self):
        current_rows = self._event_rows(self.start_date, self.end_date)
        prior_rows = self._event_rows(self.prior_start, self.prior_end)
        current = self._group(current_rows)
        prior = self._group(prior_rows)
        first_dates = self._first_event_dates() if current_rows else {}

        markets = []
        for key, g in current.items():
            cur = _group_metrics(g['rows'])
            prev = _group_metrics(prior[key]['rows']) if key in prior else None
            display = cur['metrics'][self.rank]
            prior_display = prev['metrics'][self.rank] if prev else None
            top = max(g['rows'], key=lambda r: (r['sold'], r['start_date']))
            first = first_dates.get(key)
            markets.append({
                'key': str(key) if key else 'none',
                'label': g['label'],
                # A market whose first-ever show falls in this window has no
                # audience to return yet: 0 returning is expected, not weak.
                'is_new_city': key is not None and first is not None and first >= self.start_date,
                'event_count': cur['event_count'],
                'tickets_sold': cur['tickets_sold'],
                'avg_per_event': cur['stats']['avg'],
                'median_per_event': cur['stats']['median'],
                'returning_tickets': cur['returning'],
                'new_tickets': cur['new'],
                'returning_per_event': cur['metrics'][RANK_RETURNING],
                'returning_share': cur['returning_share'],
                'new_per_1k': cur['metrics'][RANK_NEW_PER_1K],
                'fill_rate': cur['metrics'][RANK_FILL_RATE],
                'fill_week_out': cur['fill_week_out'],
                'capacity_events': cur['capacity_events'],
                # The ranked metric, its prior-window value, and the change.
                'display_value': display,
                'prior_value': prior_display,
                'prior_event_count': prev['event_count'] if prev else 0,
                'change_pct': _pct_change(display, prior_display),
                # Floats (2dp) so the payload stays JSON-safe for the chart blob.
                'ad_spend': round(cur['spend'], 2),
                'ad_spend_per_event': round(cur['spend'] / cur['event_count'], 2),
                'top_event': {
                    'id': str(top['id']),
                    'name': top['name'],
                    'sold': top['sold'],
                    'start_date': top['start_date'],
                },
            })
        # Ranked metric desc; markets without a value (e.g. no ad spend for
        # new-per-$1k) sink to the bottom.
        markets.sort(key=lambda m: (m['display_value'] is None, -(m['display_value'] or 0), m['label']))

        spent = [m for m in markets if m['ad_spend'] > 0]
        cur_all = _group_metrics(current_rows) if current_rows else None
        prev_all = _group_metrics(prior_rows) if prior_rows else None
        avg = cur_all['metrics'][RANK_TICKETS] if cur_all else None
        summary = {
            'avg_per_event': avg,
            'avg_change_pct': _pct_change(avg, prev_all['metrics'][RANK_TICKETS] if prev_all else None),
            'returning_per_event': cur_all['metrics'][RANK_RETURNING] if cur_all else None,
            'returning_share': cur_all['returning_share'] if cur_all else None,
            # Only markets that spent on ads: new buyers in no-spend markets
            # weren't bought with ad dollars, so they'd inflate efficiency.
            'new_per_1k': (
                round(sum(m['new_tickets'] for m in spent) / (sum(m['ad_spend'] for m in spent) / 1000), 1)
                if spent else None
            ),
            'fill_rate': cur_all['metrics'][RANK_FILL_RATE] if cur_all else None,
            'fill_week_out': cur_all['fill_week_out'] if cur_all else None,
            'fill_rate_change_pct': _pct_change(
                cur_all['metrics'][RANK_FILL_RATE] if cur_all else None,
                prev_all['metrics'][RANK_FILL_RATE] if prev_all else None,
            ),
            'event_count': len(current_rows),
            'tickets_sold': sum(r['sold'] for r in current_rows),
            'market_count': len(markets),
            'new_city_count': sum(1 for m in markets if m['is_new_city']),
        }
        return {
            'markets': markets,
            'summary': summary,
            'rank': self.rank,
            'start_date': self.start_date,
            'end_date': self.end_date,
            'prior_start': self.prior_start,
            'prior_end': self.prior_end,
        }
