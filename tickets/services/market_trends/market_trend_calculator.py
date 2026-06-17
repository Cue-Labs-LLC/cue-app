"""
Detects and diagnoses declining turnout per market (Event.venue.city) over time.

Turnout is measured per event as *revenue* (default), *tickets sold*, or
*profitability*. For each market we build a per-period time series, fit an
ordinary least-squares trend, classify it (declining / stable / growing), and —
for declining markets — attribute the drop to a dominant driver (fewer events,
softer demand, lower prices, higher costs, fewer new buyers, or fewer returning
buyers).

All data derives from existing Event / TicketOrder / Customer / EventExpense /
EventIncome / StripeCheckoutSession tables; no new models. A few grouped queries
+ one flat pull keep this independent of the number of markets, events, or
periods (no N+1).
"""
from datetime import date
from decimal import Decimal

import pandas as pd

from django.db.models import Count, Q, Sum
from django.db.models.functions import Coalesce, TruncMonth, TruncQuarter

from tickets.models import (
    Event, EventExpense, EventIncome, ExternalSurveyResponse,
    StripeCheckoutSession, TicketOrder,
)


# Tunable thresholds.
TREND_THRESHOLD = 0.05   # |per-period fractional change| <= this => "stable"
MIN_PERIODS = 3          # fewer observed periods => trend is not meaningful
CONFIDENCE_R2 = 0.5      # R^2 of the fit at/above this => "high" confidence
# NPS is already a fixed -100..100 scale, so its slope is normalized by the
# half-range (100) rather than by the mean — a 5pt/period slide hits TREND_THRESHOLD.
NPS_SCALE = 100.0

_VALID_METRICS = ('revenue', 'tickets', 'profitability', 'nps')

# Trailing time-window keys -> number of years to look back ('all' => no bound).
# Bounds how far back the per-period trend reads, so the slope/diagnosis reflect
# recent performance rather than all of an org's history.
_VALID_WINDOWS = {'1y': 1, '2y': 2, '3y': 3, 'all': None}

# Driver-bar labels are trend-aware: declining markets use the decline-framed
# wording, while growing/stable markets use the positive counterpart. _diagnose
# picks the set from the market's trend. (costs/detractors invert — their "good"
# direction is down, so their positive label is "Lower"/"Fewer".)
_DRIVER_LABELS = {
    'events': 'Fewer events',
    'demand': 'Lower demand per event',
    'price': 'Lower ticket prices',
    'costs': 'Higher costs per event',
    'acquisition': 'Fewer new buyers',
    'retention': 'Fewer returning buyers',
    'promoters': 'Fewer promoters',
    'detractors': 'More detractors',
}
_DRIVER_LABELS_POSITIVE = {
    'events': 'More events',
    'demand': 'Higher demand per event',
    'price': 'Higher ticket prices',
    'costs': 'Lower costs per event',
    'acquisition': 'More new buyers',
    'retention': 'More returning buyers',
    'promoters': 'More promoters',
    'detractors': 'Fewer detractors',
}


class MarketTrendCalculator:
    def __init__(self, organization, period='quarter', metric='revenue', window='2y'):
        self.organization = organization
        self.period = 'month' if period == 'month' else 'quarter'
        # The primary metric the trend is read on: revenue / tickets / profitability.
        self.metric = metric if metric in _VALID_METRICS else 'revenue'
        # Trailing window the trend reads over (1y/2y/3y/all). Defaults to 2y.
        self.window = window if window in _VALID_WINDOWS else '2y'

    # ----- period bucketing ------------------------------------------------

    def _period_key(self, d):
        """First day of the period (month or quarter) containing date `d`.

        Matches Django's TruncMonth / TruncQuarter so DB-side and Pandas-side
        bucketing agree.
        """
        if self.period == 'month':
            return date(d.year, d.month, 1)
        start_month = ((d.month - 1) // 3) * 3 + 1
        return date(d.year, start_month, 1)

    def _period_label(self, key):
        if self.period == 'month':
            return key.strftime('%b %Y')
        q = (key.month - 1) // 3 + 1
        return 'Q{} {}'.format(q, key.year)

    # ----- main entry ------------------------------------------------------

    def calculate(self):
        org = self.organization
        today = date.today()
        trunc = TruncMonth if self.period == 'month' else TruncQuarter

        # Trailing-window lower bound on the event's start_date (None => all time).
        years = _VALID_WINDOWS[self.window]
        start_cutoff = _subtract_years(today, years) if years else None

        def _date_kwargs(prefix):
            """Event-date bounds for a query whose path to Event.start_date has
            the given field `prefix` (e.g. '', 'event__', 'ticket_order__event__')."""
            kw = {prefix + 'start_date__lt': today}
            if start_cutoff is not None:
                kw[prefix + 'start_date__gte'] = start_cutoff
            return kw

        # Query A — events held per (city, period).
        events_rows = (
            Event.objects.filter(organization=org, **_date_kwargs(''))
            .annotate(period=trunc('start_date'))
            .values('venue__city', 'period')
            .annotate(events_held=Count('id'))
        )

        # Query B — tickets sold per (city, period). Counting the reverse
        # TicketOrder->Ticket relation is a single join; no Sum over a
        # different relation is mixed in, so there is no row inflation. In-person
        # (door/cash) sales ARE counted here so turnout and revenue reflect every
        # ticket sold, matching the per-event P&L. Buyer identity work below still
        # excludes in-person, since those orders carry no Customer.
        sold_rows = (
            TicketOrder.objects.filter(
                event__organization=org,
                **_date_kwargs('event__'),
            )
            .annotate(period=trunc('event__start_date'))
            .values('event__venue__city', 'period')
            .annotate(sold=Count('tickets'))
        )

        # Query B2 — gross revenue per (city, period). Summed over TicketOrder
        # only (no ticket join), so it must be a SEPARATE query from `sold` —
        # mixing Sum('total_amount') with Count('tickets') would inflate rows.
        # Includes in-person orders so revenue/profit reconcile with the
        # event-detail page and profitability_overview (which sum all orders).
        revenue_rows = (
            TicketOrder.objects.filter(
                event__organization=org,
                **_date_kwargs('event__'),
            )
            .annotate(period=trunc('event__start_date'))
            .values('event__venue__city', 'period')
            .annotate(revenue=Coalesce(Sum('total_amount'), Decimal('0.00')))
        )

        # Queries D/E/F — profit components per (city, period). Each is isolated
        # (single relation, no ticket join) and bucketed by the EVENT's start
        # date so it lines up with the revenue/tickets series.
        expense_rows = (
            EventExpense.objects.visible()
            .filter(event__organization=org, **_date_kwargs('event__'))
            .annotate(period=trunc('event__start_date'))
            .values('event__venue__city', 'period')
            .annotate(expenses=Coalesce(Sum('amount'), Decimal('0.00')))
        )
        income_rows = (
            EventIncome.objects.filter(
                deleted_at__isnull=True,
                event__organization=org,
                **_date_kwargs('event__'),
            )
            .annotate(period=trunc('event__start_date'))
            .values('event__venue__city', 'period')
            .annotate(income=Coalesce(Sum('amount'), Decimal('0.00')))
        )
        fee_rows = (
            StripeCheckoutSession.objects.filter(
                ticket_order__event__organization=org,
                ticket_order__event__ticketing_type='direct',
                **_date_kwargs('ticket_order__event__'),
            )
            .annotate(period=trunc('ticket_order__event__start_date'))
            .values('ticket_order__event__venue__city', 'period')
            .annotate(fee_cents=Coalesce(Sum('platform_fee_cents'), 0))
        )

        # Query G — NPS response counts per (city, period). Bucketed by the
        # EVENT's start_date (not the survey submission date) so NPS lines up on
        # the same period axis as the financial series. Promoters 9-10,
        # detractors 0-6 (matches services/external_survey/analytics.py).
        nps_rows = (
            ExternalSurveyResponse.objects.filter(
                organization=org,
                event__isnull=False,
                nps_score__isnull=False,
                **_date_kwargs('event__'),
            )
            .annotate(period=trunc('event__start_date'))
            .values('event__venue__city', 'period')
            .annotate(
                nps_responses=Count('id'),
                promoters=Count('id', filter=Q(nps_score__gte=9)),
                passives=Count('id', filter=Q(nps_score__gte=7, nps_score__lte=8)),
                detractors=Count('id', filter=Q(nps_score__lte=6)),
            )
        )

        # markets[city][period_key] = {events_held, sold, revenue, expenses, income, fees, nps..., ...}
        markets = {}

        def _bucket(city):
            return markets.setdefault(city or 'Unknown', {})

        for r in events_rows:
            p = _bucket(r['venue__city'])
            cell = p.setdefault(r['period'], self._empty_cell())
            cell['events_held'] = r['events_held']

        for r in sold_rows:
            p = _bucket(r['event__venue__city'])
            cell = p.setdefault(r['period'], self._empty_cell())
            cell['sold'] = r['sold']

        for r in revenue_rows:
            p = _bucket(r['event__venue__city'])
            cell = p.setdefault(r['period'], self._empty_cell())
            cell['revenue'] = float(r['revenue'] or 0)

        for r in expense_rows:
            p = _bucket(r['event__venue__city'])
            cell = p.setdefault(r['period'], self._empty_cell())
            cell['expenses'] = float(r['expenses'] or 0)

        for r in income_rows:
            p = _bucket(r['event__venue__city'])
            cell = p.setdefault(r['period'], self._empty_cell())
            cell['income'] = float(r['income'] or 0)

        for r in fee_rows:
            p = _bucket(r['ticket_order__event__venue__city'])
            cell = p.setdefault(r['period'], self._empty_cell())
            cell['fees'] = float(r['fee_cents'] or 0) / 100.0

        for r in nps_rows:
            p = _bucket(r['event__venue__city'])
            cell = p.setdefault(r['period'], self._empty_cell())
            cell['nps_responses'] = r['nps_responses']
            cell['promoters'] = r['promoters']
            cell['passives'] = r['passives']
            cell['detractors'] = r['detractors']

        # Query C — new vs returning buyers per (city, period), reusing the
        # earliest-order = "new" rule from RepeatCustomerCalculator.
        for city, key, new_count, ret_count in self._new_returning_by_market_period(today, start_cutoff):
            cell = _bucket(city).setdefault(key, self._empty_cell())
            cell['new_count'] = new_count
            cell['returning_count'] = ret_count

        market_results = []
        for city, period_map in markets.items():
            market_results.append(self._build_market(city, period_map))

        # Leaderboard order: highest to lowest by the selected metric's total.
        total_field = {
            'revenue': 'total_revenue',
            'profitability': 'total_profit',
            'tickets': 'total_sold',
            'nps': 'total_nps',
        }[self.metric]
        market_results.sort(key=lambda m: m[total_field], reverse=True)

        summary = {
            'markets_count': len(market_results),
            'declining_count': sum(1 for m in market_results if m['trend'] == 'declining'),
            'growing_count': sum(1 for m in market_results if m['trend'] == 'growing'),
            'stable_count': sum(1 for m in market_results if m['trend'] == 'stable'),
        }
        return {'period': self.period, 'window': self.window,
                'markets': market_results, 'summary': summary}

    # ----- helpers ---------------------------------------------------------

    @staticmethod
    def _empty_cell():
        return {'events_held': 0, 'sold': 0, 'revenue': 0.0,
                'expenses': 0.0, 'income': 0.0, 'fees': 0.0,
                'new_count': 0, 'returning_count': 0,
                'nps_responses': 0, 'promoters': 0, 'passives': 0, 'detractors': 0}

    def _new_returning_by_market_period(self, today, start_cutoff):
        """Yield (city, period_key, new_count, returning_count) tuples."""
        date_filter = {'event__start_date__lt': today}
        if start_cutoff is not None:
            date_filter['event__start_date__gte'] = start_cutoff
        rows = list(
            TicketOrder.objects.filter(
                customer__organization=self.organization,
                is_in_person=False,
                **date_filter,
            ).values(
                'customer_id', 'event_id', 'order_date',
                'event__venue__city', 'event__start_date',
            )
        )
        if not rows:
            return

        df = pd.DataFrame(rows)

        # Collapse to one row per (customer, event), keyed by that pair's earliest
        # order date. A buyer is "new to the org" at exactly one event — the one
        # carrying their earliest order overall (ties broken by event_id). Sorting
        # first makes this independent of DB row order; do NOT use a positional
        # cumcount on unsorted rows, which can demote the genuine first event.
        df = (
            df.groupby(
                ['customer_id', 'event_id', 'event__venue__city', 'event__start_date'],
                as_index=False,
            )['order_date'].min()
            .sort_values(['customer_id', 'order_date', 'event_id'])
        )
        df['is_new'] = ~df.duplicated('customer_id')

        df['city'] = df['event__venue__city'].fillna('Unknown')
        df['period_key'] = df['event__start_date'].apply(self._period_key)

        grouped = df.groupby(['city', 'period_key']).agg(
            total=('customer_id', 'count'),
            new_count=('is_new', 'sum'),
        ).reset_index()

        for _, r in grouped.iterrows():
            new_count = int(r['new_count'])
            returning = int(r['total']) - new_count
            yield r['city'], r['period_key'], new_count, returning

    def _build_market(self, city, period_map):
        keys = sorted(period_map.keys())
        periods = []
        for k in keys:
            cell = period_map[k]
            events_held = cell['events_held'] or 0
            sold = cell['sold'] or 0
            revenue = cell['revenue'] or 0.0
            expenses = cell['expenses'] or 0.0
            income = cell['income'] or 0.0
            fees = cell['fees'] or 0.0
            # Profit mirrors the profitability_overview view: ticket revenue
            # (all orders, incl. in-person) + other income - platform fees -
            # expenses. `cost` is the net drag.
            profit = revenue + income - fees - expenses
            cost = expenses + fees - income
            avg_sold = (sold / events_held) if events_held else 0.0
            avg_revenue = (revenue / events_held) if events_held else 0.0
            avg_profit = (profit / events_held) if events_held else 0.0
            avg_cost = (cost / events_held) if events_held else 0.0
            avg_price = (revenue / sold) if sold else 0.0
            total_buyers = cell['new_count'] + cell['returning_count']
            returning_pct = (
                round(cell['returning_count'] / total_buyers * 100, 1)
                if total_buyers else 0.0
            )
            nps_responses = cell['nps_responses'] or 0
            nps = (
                round((cell['promoters'] - cell['detractors']) / nps_responses * 100)
                if nps_responses else 0
            )
            promoter_pct = round(cell['promoters'] / nps_responses * 100, 1) if nps_responses else 0.0
            detractor_pct = round(cell['detractors'] / nps_responses * 100, 1) if nps_responses else 0.0
            periods.append({
                'period_key': k.isoformat(),
                'period_label': self._period_label(k),
                'events_held': events_held,
                'sold': sold,
                'revenue': round(revenue, 2),
                'profit': round(profit, 2),
                'avg_sold_per_event': round(avg_sold, 1),
                'avg_revenue_per_event': round(avg_revenue, 2),
                'avg_profit_per_event': round(avg_profit, 2),
                'avg_cost_per_event': round(avg_cost, 2),
                'avg_price': round(avg_price, 2),
                'new_count': cell['new_count'],
                'returning_count': cell['returning_count'],
                'returning_pct': returning_pct,
                'nps_responses': nps_responses,
                'nps': nps,
                'promoter_pct': promoter_pct,
                'detractor_pct': detractor_pct,
            })

        # The series the trend is read on depends on the selected metric.
        series_field = {
            'revenue': 'avg_revenue_per_event',
            'tickets': 'avg_sold_per_event',
            'profitability': 'avg_profit_per_event',
            'nps': 'nps',
        }[self.metric]
        is_nps = self.metric == 'nps'
        # Which periods carry signal depends on the metric: NPS needs survey
        # responses, the financial metrics need events. The sparkline and detail
        # chart plot exactly these periods so the trend line aligns.
        observe_field = 'nps_responses' if is_nps else 'events_held'
        observed = [p for p in periods if p[observe_field] > 0]
        sparkline_periods = observed or periods

        # Overall NPS across all of the market's responses.
        tot_resp = sum(period_map[k]['nps_responses'] for k in keys)
        tot_prom = sum(period_map[k]['promoters'] for k in keys)
        tot_detr = sum(period_map[k]['detractors'] for k in keys)
        total_nps = round((tot_prom - tot_detr) / tot_resp * 100) if tot_resp else 0
        result = {
            'city': city,
            'metric': self.metric,
            'change_unit': 'pts' if is_nps else '%',
            'periods': periods,
            'sparkline': [p[series_field] for p in sparkline_periods],
            'total_sold': sum(p['sold'] for p in periods),
            'total_revenue': round(sum(p['revenue'] for p in periods), 2),
            'total_profit': round(sum(p['profit'] for p in periods), 2),
            'total_nps': total_nps,
            'total_responses': tot_resp,
            'total_events': sum(p['events_held'] for p in periods),
            'trend': 'insufficient_data',
            'norm_slope_pct': 0.0,
            'confidence': None,
            'trend_line': [],
            'dominant_driver': None,
            'driver_contributions': [],
            'diagnosis_text': '',
            'recommended_action': None,
        }

        if len(observed) < MIN_PERIODS:
            return result

        ys = [p[series_field] for p in observed]
        slope, intercept, mean_y, r2 = _ols(ys)
        # NPS normalizes by its fixed half-range; financial metrics by |mean| so a
        # worsening loss (negative mean profit) still reads as declining.
        denom = NPS_SCALE if is_nps else abs(mean_y)
        norm_slope = (slope / denom) if denom else 0.0

        result['norm_slope_pct'] = round(norm_slope * 100, 1)
        result['confidence'] = 'high' if r2 >= CONFIDENCE_R2 else 'low'
        result['trend_line'] = [round(intercept + slope * i, 1) for i in range(len(ys))]

        if norm_slope > TREND_THRESHOLD:
            result['trend'] = 'growing'
        elif norm_slope < -TREND_THRESHOLD:
            result['trend'] = 'declining'
        else:
            result['trend'] = 'stable'

        if result['trend'] in ('declining', 'growing', 'stable'):
            self._diagnose(result, observed)

        return result

    def _diagnose(self, result, observed):
        """Build per-driver contributions and a next step for any trend
        direction (first third vs last third)."""
        n = len(observed)
        third = max(1, n // 3)
        first = observed[:third]
        last = observed[-third:]

        def _mean(rows, field):
            return sum(r[field] for r in rows) / len(rows) if rows else 0.0

        # Candidate drivers depend on the metric. `hurts_when` records the
        # direction that drags the metric down: 'down' for the usual metrics
        # (a fall hurts) and 'up' for costs (a rise hurts profit).
        if self.metric == 'nps':
            # NPS = promoter share - detractor share; decompose into those two.
            candidates = [
                ('promoters', 'promoter_pct', 'down'),
                ('detractors', 'detractor_pct', 'up'),
            ]
        else:
            candidates = [
                ('events', 'events_held', 'down'),
                ('demand', 'avg_sold_per_event', 'down'),
            ]
            if self.metric in ('revenue', 'profitability'):
                candidates.append(('price', 'avg_price', 'down'))
            if self.metric == 'profitability':
                candidates.append(('costs', 'avg_cost_per_event', 'up'))
            candidates += [
                ('acquisition', 'new_count', 'down'),
                ('retention', 'returning_pct', 'down'),
            ]

        # Negative-framed labels for declining markets; positive counterparts for
        # growing/stable markets.
        labels = (_DRIVER_LABELS_POSITIVE if result['trend'] in ('growing', 'stable')
                  else _DRIVER_LABELS)

        drivers = []
        for key, field, hurts_when in candidates:
            a = _mean(first, field)
            b = _mean(last, field)
            change = ((b - a) / a) if a else 0.0
            drivers.append({
                'key': key,
                'label': labels[key],
                'change_pct': round(change * 100, 1),
                'first': round(a, 1),
                'last': round(b, 1),
                'hurts_when': hurts_when,
            })

        result['driver_contributions'] = drivers
        # Rank drivers to pick the badged lead by their estimated CONTRIBUTION to
        # the per-event series change, not raw % movement (a ratio like returning
        # share can swing wildly in % from a small base and isn't what moved the
        # metric). Revenue/Profit: $-contribution (revenue/event = demand x price;
        # profit nets out cost). Tickets: change in new- vs returning-buyer COUNT
        # per event (demand is the aggregate series, events isn't a per-event
        # factor, so both stay context bars at rank 0). NPS: relative % of the two
        # symmetric shares. Positive rank => helps the metric, negative => hurts.
        by_key = {d['key']: d for d in drivers}

        def _rank(d):
            if self.metric in ('revenue', 'profitability'):
                return self._dollar_contribution(d['key'], by_key)
            if self.metric == 'tickets':
                return self._ticket_contribution(d['key'], first, last)
            return d['change_pct'] if d['hurts_when'] == 'down' else -d['change_pct']

        trend = result['trend']
        if trend == 'declining':
            dominant = min(drivers, key=_rank)
            if _rank(dominant) >= 0:
                # Decline isn't cleanly attributable to one declining driver.
                result['dominant_driver'] = None
                result['diagnosis_text'] = (
                    '{} in {} is trending down ~{}{} per {}, but no single driver '
                    'stands out — worth a closer look.'.format(
                        self._metric_noun(), result['city'],
                        abs(result['norm_slope_pct']), self._change_unit(), self.period,
                    )
                )
                result['recommended_action'] = self._growth_action()
                return
            result['dominant_driver'] = dominant['key']
            result['diagnosis_text'] = self._diagnosis_text(result, dominant)
            result['recommended_action'] = self._recommended_action(dominant['key'])
        elif trend == 'growing':
            lead = max(drivers, key=_rank)
            if _rank(lead) > 0:
                result['dominant_driver'] = lead['key']
                result['diagnosis_text'] = self._growth_text(result, lead)
                result['recommended_action'] = self._recommended_action(lead['key'], mode='reinforce')
            else:
                result['dominant_driver'] = None
                result['diagnosis_text'] = (
                    '{} in {} is up ~{}{} per {} — keep doing what works here.'.format(
                        self._metric_noun(), result['city'],
                        abs(result['norm_slope_pct']), self._change_unit(), self.period,
                    )
                )
                result['recommended_action'] = self._growth_action()
        else:  # stable
            result['dominant_driver'] = None
            result['diagnosis_text'] = self._steady_text(result)
            result['recommended_action'] = self._growth_action()

    def _dollar_contribution(self, key, by_key):
        """Estimated effect of a driver on the per-event series change, in $.

        revenue/event = demand x price, so a first-order split of the change is
        Δdemand·price_avg + Δprice·demand_avg (the interaction term is shared
        evenly and the two sum to the revenue change). Profit nets out the cost
        per event. Audience/volume drivers (events, new/returning buyers) are not
        factors of the per-event series, so they contribute 0 and never get badged.
        """
        demand = by_key.get('demand')
        price = by_key.get('price')
        if not demand or not price:
            return 0.0
        d0, d1 = demand['first'], demand['last']
        p0, p1 = price['first'], price['last']
        if key == 'demand':
            return (d1 - d0) * (p0 + p1) / 2.0
        if key == 'price':
            return (p1 - p0) * (d0 + d1) / 2.0
        if key == 'costs':
            costs = by_key.get('costs')
            return -(costs['last'] - costs['first']) if costs else 0.0
        return 0.0  # events / acquisition / retention — context only

    @staticmethod
    def _ticket_contribution(key, first, last):
        """Change in a buyer segment's COUNT per event (first third vs last).

        Tickets/event are filled by new + returning buyers, so the volume change
        is attributed to whichever segment's per-event count moved more — putting
        new and returning on equal footing (counts), instead of the returning
        SHARE ratio that swings hard from a small base. `demand` is the aggregate
        series itself and `events` isn't a per-event factor, so both rank 0 and
        stay context bars. (Approximate: ignores tickets-per-buyer drift.)
        """
        field = {'acquisition': 'new_count', 'retention': 'returning_count'}.get(key)
        if not field:
            return 0.0

        def _per_event(rows):
            vals = [r[field] / r['events_held'] for r in rows if r['events_held']]
            return sum(vals) / len(vals) if vals else 0.0

        return _per_event(last) - _per_event(first)

    def _metric_noun(self):
        """Display-ready, properly-cased noun for the selected metric."""
        return {
            'revenue': 'Revenue',
            'profitability': 'Profit',
            'nps': 'NPS',
        }.get(self.metric, 'Turnout')

    def _change_unit(self):
        return 'pts' if self.metric == 'nps' else '%'

    def _diagnosis_text(self, result, dominant):
        drop = abs(result['norm_slope_pct'])
        city = result['city']
        key = dominant['key']
        if key == 'events':
            phrase = 'fewer events on the calendar (about {} down to {} per {})'.format(
                dominant['first'], dominant['last'], self.period,
            )
        elif key == 'demand':
            phrase = 'softer demand per event (avg tickets sold fell {}→{})'.format(
                dominant['first'], dominant['last'],
            )
        elif key == 'price':
            phrase = 'lower ticket prices (avg fell ${}→${})'.format(
                dominant['first'], dominant['last'],
            )
        elif key == 'costs':
            phrase = 'higher costs per event (avg rose ${}→${})'.format(
                dominant['first'], dominant['last'],
            )
        elif key == 'acquisition':
            phrase = 'fewer new buyers entering the market ({}→{} per {})'.format(
                dominant['first'], dominant['last'], self.period,
            )
        elif key == 'promoters':
            phrase = 'fewer promoters (9-10 share fell {}%→{}%)'.format(
                dominant['first'], dominant['last'],
            )
        elif key == 'detractors':
            phrase = 'more detractors (0-6 share rose {}%→{}%)'.format(
                dominant['first'], dominant['last'],
            )
        else:  # retention
            phrase = 'fewer returning buyers (repeat mix fell {}%→{}%)'.format(
                dominant['first'], dominant['last'],
            )
        return '{} in {} is down ~{}{} per {}, driven mainly by {}.'.format(
            self._metric_noun(), city, drop, self._change_unit(), self.period, phrase,
        )

    def _growth_text(self, result, lead):
        return '{} in {} is up ~{}{} per {}, led by {}.'.format(
            self._metric_noun(), result['city'], abs(result['norm_slope_pct']),
            self._change_unit(), self.period, self._boost_phrase(lead),
        )

    def _steady_text(self, result):
        lever = ('turning passives into promoters' if self.metric == 'nps'
                 else 'bringing in new buyers')
        return '{} in {} is holding steady (~{}{} per {}). Best lever to grow: {}.'.format(
            self._metric_noun(), result['city'], abs(result['norm_slope_pct']),
            self._change_unit(), self.period, lever,
        )

    def _boost_phrase(self, d):
        """Positive-framed phrase for the driver leading a market's gains."""
        key = d['key']
        if key == 'events':
            return 'more events on the calendar (about {} up to {} per {})'.format(
                d['first'], d['last'], self.period,
            )
        if key == 'demand':
            return 'stronger demand per event (avg tickets sold rose {}→{})'.format(
                d['first'], d['last'],
            )
        if key == 'price':
            return 'higher ticket prices (avg ${}→${})'.format(d['first'], d['last'])
        if key == 'costs':
            return 'lower costs per event (avg ${}→${})'.format(d['first'], d['last'])
        if key == 'acquisition':
            return 'more new buyers entering the market ({}→{} per {})'.format(
                d['first'], d['last'], self.period,
            )
        if key == 'promoters':
            return 'more promoters (9-10 share rose {}%→{}%)'.format(d['first'], d['last'])
        if key == 'detractors':
            return 'fewer detractors (0-6 share fell {}%→{}%)'.format(d['first'], d['last'])
        return 'a stronger repeat mix ({}%→{}%)'.format(d['first'], d['last'])  # retention

    def _recommended_action(self, driver_key, mode='fix'):
        if mode == 'reinforce':
            return self._reinforce_action(driver_key)
        return self._fix_action(driver_key)

    def _growth_action(self):
        """Default 'how to grow from here' step for stable / flat markets."""
        if self.metric == 'nps':
            return {
                'key': 'promoters',
                'label': 'Review survey feedback',
                'url_name': 'tickets:survey_analytics',
                'icon': 'bi-chat-square-heart',
                'note': 'Sentiment is steady — keep gathering feedback to turn '
                        'passives into promoters.',
            }
        return {
            'key': 'acquisition',
            'label': 'Review customer segments',
            'url_name': 'tickets:customer_segments',
            'icon': 'bi-diagram-3',
            'note': 'Steady is good — to grow from here, focus on bringing in new buyers.',
        }

    @staticmethod
    def _reinforce_action(driver_key):
        """Same tool as the fix step, framed as 'keep the momentum going'."""
        tools = {
            'retention': ('Review retention & win-backs', 'tickets:churn_overview', 'bi-person-dash'),
            'acquisition': ('Review customer segments', 'tickets:customer_segments', 'bi-diagram-3'),
            'demand': ('Open the forecast tool', 'tickets:forecast_tool', 'bi-graph-up-arrow'),
            'costs': ('Review event expenses', 'tickets:expense_analytics', 'bi-receipt-cutoff'),
            'promoters': ('Review survey feedback', 'tickets:survey_analytics', 'bi-chat-square-heart'),
            'detractors': ('Review survey feedback', 'tickets:survey_analytics', 'bi-chat-square-heart'),
        }
        notes = {
            'events': 'More events are lifting this market — keep the calendar full.',
            'demand': 'Demand is leading the gains — line up capacity and dates to keep it climbing.',
            'price': 'Pricing power is strong here — hold it and test premium tiers.',
            'costs': 'Costs are trending down — bank the margin and keep them lean.',
            'acquisition': 'New-buyer growth is your engine here — keep feeding the funnel.',
            'retention': 'Repeat buyers are powering growth — protect them with loyalty and win-backs.',
            'promoters': 'Promoters are climbing — keep gathering feedback and amplify what they love.',
            'detractors': 'Detractors are shrinking — keep closing the loop on feedback.',
        }
        label, url_name, icon = tools.get(driver_key, (None, None, 'bi-stars'))
        return {
            'key': driver_key,
            'label': label,
            'url_name': url_name,
            'icon': icon,
            'note': notes.get(driver_key, ''),
        }

    @staticmethod
    def _fix_action(driver_key):
        if driver_key == 'retention':
            return {
                'key': 'retention',
                'label': 'Win back lapsed buyers',
                'url_name': 'tickets:churn_overview',
                'icon': 'bi-person-dash',
            }
        if driver_key == 'acquisition':
            return {
                'key': 'acquisition',
                'label': 'Review customer segments',
                'url_name': 'tickets:customer_segments',
                'icon': 'bi-diagram-3',
            }
        if driver_key == 'demand':
            return {
                'key': 'demand',
                'label': 'Open the forecast tool',
                'url_name': 'tickets:forecast_tool',
                'icon': 'bi-graph-up-arrow',
            }
        if driver_key == 'price':
            # No pricing tool exists — surface as guidance only.
            return {
                'key': 'price',
                'label': None,
                'note': 'Average ticket price in this market has slipped. Revisit '
                        'pricing and tier structure for upcoming shows here.',
                'url_name': None,
                'icon': 'bi-tag',
            }
        if driver_key == 'costs':
            return {
                'key': 'costs',
                'label': 'Review event expenses',
                'url_name': 'tickets:expense_analytics',
                'icon': 'bi-receipt-cutoff',
            }
        if driver_key in ('promoters', 'detractors'):
            return {
                'key': driver_key,
                'label': 'Review survey feedback',
                'url_name': 'tickets:survey_analytics',
                'icon': 'bi-chat-square-heart',
            }
        # 'events' — programming/booking issue, informational only.
        return {
            'key': 'events',
            'label': None,
            'note': 'Turnout is slipping because fewer events are on the calendar '
                    'here. Consider booking more dates in this market.',
            'url_name': None,
            'icon': 'bi-calendar-plus',
        }


def _subtract_years(d, n):
    """Return the date `n` years before `d`, clamping Feb 29 -> Feb 28."""
    try:
        return d.replace(year=d.year - n)
    except ValueError:
        return d.replace(year=d.year - n, day=28)


def _ols(ys):
    """Ordinary least-squares fit of ys against x = 0,1,2,...

    Returns (slope, intercept, mean_y, r_squared). Pure Python — no numpy
    needed and no failure on a perfectly flat series.
    """
    n = len(ys)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    slope = (sxy / sxx) if sxx else 0.0
    intercept = mean_y - slope * mean_x

    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    if ss_tot == 0:
        r2 = 1.0  # perfectly flat line — a flat fit explains it exactly
    else:
        ss_res = sum((ys[i] - (intercept + slope * xs[i])) ** 2 for i in range(n))
        r2 = 1 - ss_res / ss_tot
    return slope, intercept, mean_y, r2
