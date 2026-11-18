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

Ad spend is each event's marketing expenses (manual + Meta Ads lines, both
category='marketing'), summed per market. It is not affected by the ticket
filter: spend is per event, not per ticket.

Spend-adjusted demand
---------------------
Raw ticket counts mix organic demand with what ads bought: two shows that each
sold 200 tickets aren't equally in demand if one needed $1,000 of ads and the
other $100. The spend-adjusted view estimates what each event would have sold
with no ad spend:

    adjusted = max(0, sold - beta * ln(1 + spend))

beta is fit per org (see ``fit_spend_effect``) from its own events over the last
few years: the within-market slope of tickets on ln(1 + spend). ln gives
diminishing returns (each doubling of spend adds ~beta * ln 2 tickets); fitting
within each market keeps big markets, which naturally get bigger budgets, from
inflating the effect. Zero-spend events are unchanged. With too little history
(or no spend variation) the model is unavailable and the page shows raw counts.
"""
import math
from datetime import date, timedelta

from django.db.models import Count, IntegerField, OuterRef, Subquery
from django.db.models.functions import Coalesce

from tickets.models import EVENT_STATUS_CANCELLED, Event, Ticket
from tickets.services.conversion import _marketing_spend_expr, _stats
from tickets.services.markets import NO_MARKET_LABEL


TICKET_FILTER_ALL = 'all'
TICKET_FILTER_PAID = 'paid'
TICKET_FILTER_FREE = 'free'
VALID_TICKET_FILTERS = (TICKET_FILTER_ALL, TICKET_FILTER_PAID, TICKET_FILTER_FREE)

DEMAND_MODE_RAW = 'raw'
DEMAND_MODE_ADJUSTED = 'adjusted'
VALID_DEMAND_MODES = (DEMAND_MODE_RAW, DEMAND_MODE_ADJUSTED)

# Spend-effect model inputs: how far back to learn from, and the minimum history
# needed before the estimate is trusted enough to show.
SPEND_MODEL_LOOKBACK_YEARS = 3
SPEND_MODEL_MIN_EVENTS = 15
SPEND_MODEL_MIN_SPEND_EVENTS = 5


def normalize_ticket_filter(value):
    """Return a valid ticket filter key, falling back to 'all'."""
    return value if value in VALID_TICKET_FILTERS else TICKET_FILTER_ALL


def normalize_demand_mode(value):
    """Return a valid demand mode, defaulting to spend-adjusted."""
    return value if value in VALID_DEMAND_MODES else DEMAND_MODE_ADJUSTED


def _pct_change(current, prior):
    """Percent change from prior to current, or None when there is no usable prior."""
    if current is None or not prior:
        return None
    return round((current - prior) / prior * 100, 1)


def _spend_x(spend):
    return math.log1p(max(float(spend), 0.0))


def fit_spend_effect(rows):
    """Estimate how many tickets ad spend buys per event, from past event rows.

    Each row needs 'market_id', 'sold' and 'ad_spend'. Fits
    ``sold = market baseline + beta * ln(1 + spend)`` by OLS with market fixed
    effects (the "within" estimator: demean x and y inside each market, then
    regress). Returns a dict with:

      available  -- True when there is enough history to trust beta
      beta       -- tickets per unit of ln(1 + spend), clamped >= 0
      tickets_per_doubling -- beta * ln 2, the plain-language effect size
      events / spend_events -- sample sizes behind the fit
      reason     -- why it's unavailable ('' when available)
    """
    events = len(rows)
    spend_events = sum(1 for r in rows if r['ad_spend'] and r['ad_spend'] > 0)
    result = {
        'available': False, 'beta': 0.0, 'tickets_per_doubling': 0.0,
        'events': events, 'spend_events': spend_events, 'reason': '',
    }
    if events < SPEND_MODEL_MIN_EVENTS or spend_events < SPEND_MODEL_MIN_SPEND_EVENTS:
        result['reason'] = 'not_enough_history'
        return result

    groups = {}
    for r in rows:
        groups.setdefault(r['market_id'], []).append((_spend_x(r['ad_spend']), float(r['sold'])))

    num = den = 0.0
    for points in groups.values():
        if len(points) < 2:
            continue  # a single-event market says nothing about spend within it
        x_mean = sum(x for x, _ in points) / len(points)
        y_mean = sum(y for _, y in points) / len(points)
        for x, y in points:
            num += (x - x_mean) * (y - y_mean)
            den += (x - x_mean) ** 2
    if den <= 0:
        # Spend never varies within a market, so its effect can't be separated
        # from the market itself.
        result['reason'] = 'no_spend_variation'
        return result

    # Ads can't plausibly reduce sales; a negative slope means spend tracks
    # weak shows (organizers boosting slow sellers), so treat it as no effect.
    beta = max(num / den, 0.0)
    result.update(
        available=True,
        beta=beta,
        tickets_per_doubling=round(beta * math.log(2), 1),
    )
    return result


def adjusted_sold(sold, ad_spend, beta):
    """Estimated tickets an event would have sold with no ad spend (floored at 0)."""
    return max(0.0, float(sold) - beta * _spend_x(ad_spend))


class MarketDemandCalculator:
    def __init__(self, organization, start_date, end_date, ticket_filter=TICKET_FILTER_ALL,
                 demand_mode=DEMAND_MODE_ADJUSTED):
        self.organization = organization
        self.ticket_filter = normalize_ticket_filter(ticket_filter)
        self.demand_mode = normalize_demand_mode(demand_mode)
        today = date.today()
        self.today = today
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
            # Each stat is its own Subquery, so tickets and spend never inflate each other.
            .annotate(sold=Coalesce(sold_subq, 0), ad_spend=_marketing_spend_expr())
            .values('id', 'name', 'start_date', 'market_id', 'market__name', 'sold', 'ad_spend')
        )

    def _fit_rows(self):
        """Past events the spend model learns from (same ticket filter as the view)."""
        try:
            start = self.today.replace(year=self.today.year - SPEND_MODEL_LOOKBACK_YEARS)
        except ValueError:  # Feb 29
            start = self.today.replace(year=self.today.year - SPEND_MODEL_LOOKBACK_YEARS, day=28)
        return self._event_rows(start, self.today)

    @staticmethod
    def _group(rows):
        """Group per-event rows by market id -> {'label', 'sold', 'adjusted', 'rows'}."""
        groups = {}
        for row in rows:
            key = row['market_id']
            label = (row['market__name'] or '').strip() or NO_MARKET_LABEL
            g = groups.setdefault(key, {'label': label, 'sold': [], 'adjusted': [], 'rows': []})
            g['sold'].append(row['sold'])
            g['adjusted'].append(row['adjusted'])
            g['rows'].append(row)
        return groups

    def calculate(self):
        current_rows = self._event_rows(self.start_date, self.end_date)
        prior_rows = self._event_rows(self.prior_start, self.prior_end)
        model = fit_spend_effect(self._fit_rows()) if current_rows else fit_spend_effect([])
        beta = model['beta'] if model['available'] else 0.0
        for row in current_rows + prior_rows:
            row['adjusted'] = adjusted_sold(row['sold'], row['ad_spend'], beta)

        # Fall back to raw counts when the model can't be trusted.
        adjusted = self.demand_mode == DEMAND_MODE_ADJUSTED and model['available']
        mode = DEMAND_MODE_ADJUSTED if adjusted else DEMAND_MODE_RAW
        shown = 'adjusted' if adjusted else 'sold'

        current = self._group(current_rows)
        prior = self._group(prior_rows)

        markets = []
        for key, g in current.items():
            raw = _stats(g['sold'], 1)
            adj = _stats(g['adjusted'], 1)
            disp = adj if adjusted else raw
            prior_disp = _stats(prior[key][shown], 1)['avg'] if key in prior else None
            top = max(g['rows'], key=lambda r: (r['sold'], r['start_date']))
            ad_spend = float(sum(r['ad_spend'] for r in g['rows']))
            tickets_sold = sum(g['sold'])
            markets.append({
                'key': str(key) if key else 'none',
                'label': g['label'],
                'event_count': len(g['sold']),
                'tickets_sold': tickets_sold,
                # Raw tickets per event, always available.
                'avg_per_event': raw['avg'],
                'median_per_event': raw['median'],
                # Spend-adjusted tickets per event (equals raw when the model is off).
                'adjusted_avg': adj['avg'],
                'adjusted_median': adj['median'],
                # What the page leads with, per the active mode.
                'display_avg': disp['avg'],
                'display_median': disp['median'],
                'prior_avg': prior_disp,
                'prior_event_count': len(prior[key]['sold']) if key in prior else 0,
                'change_pct': _pct_change(disp['avg'], prior_disp),
                # Floats (2dp) so the payload stays JSON-safe for the chart blob.
                'ad_spend': round(ad_spend, 2),
                'ad_spend_per_event': round(ad_spend / len(g['rows']), 2),
                'cost_per_ticket': (
                    round(ad_spend / tickets_sold, 2) if ad_spend > 0 and tickets_sold > 0 else None
                ),
                'top_event': {
                    'id': str(top['id']),
                    'name': top['name'],
                    'sold': top['sold'],
                    'start_date': top['start_date'],
                },
            })
        markets.sort(key=lambda m: (-m['display_avg'], m['label']))

        current_disp = [r[shown] for r in current_rows]
        prior_disp_all = [r[shown] for r in prior_rows]
        overall = _stats(current_disp, 1)['avg']
        prior_overall = _stats(prior_disp_all, 1)['avg']
        summary = {
            'display_avg': overall,
            'raw_avg': _stats([r['sold'] for r in current_rows], 1)['avg'],
            'prior_avg': prior_overall,
            'change_pct': _pct_change(overall, prior_overall),
            'event_count': len(current_rows),
            'tickets_sold': sum(r['sold'] for r in current_rows),
            'market_count': len(markets),
        }
        return {
            'markets': markets,
            'summary': summary,
            'demand_mode': mode,
            'spend_model': model,
            'start_date': self.start_date,
            'end_date': self.end_date,
            'prior_start': self.prior_start,
            'prior_end': self.prior_end,
        }
