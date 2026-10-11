"""
Overall conversion-rate analysis.

Compares how well each event's buy page converts page views into orders.
Conversion rate for an event is ``orders / buy-page views``, counted the same way
as the per-event Conversion Rate stat tile on the event Analytics tab
(``total_orders`` = ``Count(ticket_orders)``, ``views`` =
``Event.public_buy_page_views``) so the two reconcile.

An event qualifies when it has recorded buy-page views (``public_buy_page_views >
0``) AND its page views are shown at all — the same rule as everywhere else
(``tickets/views.py`` ``_annotate_event_financials``): direct-ticketing events
always, CSV (external) events only when the org has opted in via
``Organization.show_page_views_for_external_events`` (their view counts are entered
manually). This is the "when pageview data is available" gate.

Returns one row per qualifying event (chronological) plus an org-wide summary,
suitable for a bar chart with one bar per event.

``market_comparison()`` reduces the same qualifying events to one row per market
(average / median of the per-event conversion rate and marketing cost ratios),
for a cross-market comparison card.
"""
from decimal import Decimal

import numpy as np
from django.db.models import Count, DecimalField, OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce

from tickets.models import Event, EventExpense, TICKETING_TYPE_DIRECT
from tickets.services.markets import NO_MARKET_LABEL


def _marketing_spend_expr():
    """Per-event marketing spend as an isolated Subquery.

    Summed in its own Subquery so the Sum over the expenses table never multiplies
    rows against a ticket_orders join (the house rule for mixing Count + Sum).
    Covers manual + Meta Ads marketing line items (both stored category='marketing').
    """
    marketing_subq = (
        EventExpense.objects.filter(
            event=OuterRef('pk'),
            category='marketing',
            deleted_at__isnull=True,
        )
        .values('event')
        .annotate(total=Sum('amount'))
        .values('total')
    )
    return Coalesce(
        Subquery(marketing_subq, output_field=DecimalField(max_digits=12, decimal_places=2)),
        Decimal('0.00'),
    )


def _stats(values, ndigits):
    """Average + median of a list of numbers, rounded to ndigits (floats, JSON-safe).

    Returns {'avg': None, 'median': None} for an empty list.
    """
    if not values:
        return {'avg': None, 'median': None}
    return {
        'avg': round(float(np.mean(values)), ndigits),
        'median': round(float(np.median(values)), ndigits),
    }


class ConversionRateCalculator:
    """Per-event buy-page conversion rate for an org.

    market_id: restrict to events in a single market.
    no_market: restrict to events with no market assigned.
    start_date / end_date: display window applied to each event's own start_date
        (selects which events appear — this is a per-event comparison, not a time
        series).
    """

    def __init__(self, organization, market_id=None, no_market=False,
                 start_date=None, end_date=None):
        self.organization = organization
        self.market_id = market_id
        self.no_market = no_market
        self.start_date = start_date
        self.end_date = end_date

    def _empty(self):
        return {
            'events': [],
            'summary': {
                'overall_rate': None,
                'total_orders': 0,
                'total_views': 0,
                'event_count': 0,
                'total_marketing_spend': Decimal('0.00'),
                'overall_cost_per_view': None,
                'overall_cost_per_order': None,
            },
        }

    def _base_queryset(self):
        """Qualifying events for this org + date window, across ALL markets.

        Gate: events with recorded buy-page views whose views are shown at all —
        direct always, external only when the org has opted in. The window trims
        which events appear, by the event's own start_date. The single-market
        filter is intentionally NOT applied here (``calculate`` adds it;
        ``market_comparison`` spans all markets).
        """
        events = Event.objects.filter(
            organization=self.organization,
            public_buy_page_views__gt=0,
        )
        if not self.organization.show_page_views_for_external_events:
            events = events.filter(ticketing_type=TICKETING_TYPE_DIRECT)
        if self.start_date is not None:
            events = events.filter(start_date__gte=self.start_date)
        if self.end_date is not None:
            events = events.filter(start_date__lte=self.end_date)
        return events

    def calculate(self):
        events = self._base_queryset()
        if self.no_market:
            events = events.filter(market__isnull=True)
        elif self.market_id:
            events = events.filter(market_id=self.market_id)

        # order_count is a single Count on one related table (no join inflation);
        # marketing spend is an isolated Subquery (see _marketing_spend_expr). views
        # is read from the field directly, so no join at all.
        rows = list(
            events.annotate(
                order_count=Count('ticket_orders'),
                marketing_spend=_marketing_spend_expr(),
            )
            .order_by('start_date')
            .values('id', 'name', 'start_date', 'order_count',
                    'public_buy_page_views', 'marketing_spend')
        )
        if not rows:
            return self._empty()

        event_data = []
        total_orders = 0
        total_views = 0
        total_marketing_spend = Decimal('0.00')
        for row in rows:
            orders = row['order_count']
            views = row['public_buy_page_views']
            spend = row['marketing_spend']
            total_orders += orders
            total_views += views
            total_marketing_spend += spend
            event_data.append({
                'event_id': str(row['id']),
                'name': row['name'],
                'start_date': row['start_date'],
                'orders': orders,
                'views': views,
                'conversion_rate': round(orders / views * 100, 1) if views > 0 else 0.0,
                'marketing_spend': spend,
                # Cost per view is often sub-cent, so keep 4 dp; per order keeps 2 dp.
                'cost_per_view': (spend / views).quantize(Decimal('0.0001')) if views > 0 else None,
                'cost_per_order': (spend / orders).quantize(Decimal('0.01')) if orders > 0 else None,
            })

        # Overall rate is the aggregate ratio, not the average of per-event rates,
        # so events with more traffic weigh more (matches the point-in-time tile).
        # The marketing cost ratios are aggregated the same way (total spend over
        # total views / orders), not an average of per-event ratios.
        overall_rate = round(total_orders / total_views * 100, 1) if total_views > 0 else None
        overall_cost_per_view = (
            (total_marketing_spend / total_views).quantize(Decimal('0.0001'))
            if total_views > 0 else None
        )
        overall_cost_per_order = (
            (total_marketing_spend / total_orders).quantize(Decimal('0.01'))
            if total_orders > 0 else None
        )

        return {
            'events': event_data,
            'summary': {
                'overall_rate': overall_rate,
                'total_orders': total_orders,
                'total_views': total_views,
                'event_count': len(event_data),
                'total_marketing_spend': total_marketing_spend,
                'overall_cost_per_view': overall_cost_per_view,
                'overall_cost_per_order': overall_cost_per_order,
            },
        }

    def market_comparison(self):
        """One row per market comparing the central tendency of its events.

        Spans ALL markets in the window (the instance's single-market filter is
        intentionally ignored — this is a cross-market comparison). For each market
        returns the average AND median of its events' per-event conversion rate and
        marketing cost ratios, so the UI can toggle avg/median client-side without a
        round-trip. Unlike the ``summary`` tiles (traffic-weighted overall ratios),
        these are the unweighted central tendency of the per-event values.

        Cost per order excludes events with no orders; a market whose events all have
        zero orders reports ``cpo = {'avg': None, 'median': None}`` (and drops out of
        the $/Order view client-side).
        """
        rows = list(
            self._base_queryset()
            .annotate(
                order_count=Count('ticket_orders'),
                marketing_spend=_marketing_spend_expr(),
            )
            .values('order_count', 'public_buy_page_views', 'marketing_spend',
                    'market_id', 'market__name')
        )

        # Group per-event values by market (None market_id -> the "No market" bucket).
        groups = {}
        for row in rows:
            views = row['public_buy_page_views']
            orders = row['order_count']
            spend = float(row['marketing_spend'])
            key = row['market_id']
            label = (row['market__name'] or '').strip() or NO_MARKET_LABEL
            g = groups.setdefault(key, {'label': label, 'rates': [], 'cpvs': [], 'cpos': []})
            # views > 0 for every qualifying event (gate), so rate/cpv are always defined.
            g['rates'].append(orders / views * 100 if views > 0 else 0.0)
            if views > 0:
                g['cpvs'].append(spend / views)
            if orders > 0:
                g['cpos'].append(spend / orders)

        markets = [
            {
                'label': g['label'],
                'event_count': len(g['rates']),
                'rate': _stats(g['rates'], 1),
                'cpv': _stats(g['cpvs'], 4),
                'cpo': _stats(g['cpos'], 2),
            }
            for g in groups.values()
        ]
        # Stable server order by label; the client re-sorts by the selected metric.
        markets.sort(key=lambda m: m['label'])
        return markets
