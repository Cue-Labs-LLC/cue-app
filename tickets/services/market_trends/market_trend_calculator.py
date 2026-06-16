"""
Detects and diagnoses declining turnout per market (Event.venue.city) over time.

Turnout is measured as *tickets sold per event* — always available, no scan
dependency. For each market we build a per-period time series, fit an ordinary
least-squares trend, classify it (declining / stable / growing), and — for
declining markets — attribute the drop to a dominant driver (fewer events,
softer demand, fewer new buyers, or fewer returning buyers).

All data derives from existing Event / TicketOrder / Customer tables; no new
models. Three grouped queries + one flat pull keep this independent of the
number of markets, events, or periods (no N+1).
"""
from datetime import date

import pandas as pd

from django.db.models import Count
from django.db.models.functions import TruncMonth, TruncQuarter

from tickets.models import Event, TicketOrder


# Tunable thresholds.
TREND_THRESHOLD = 0.05   # |per-period fractional change| <= this => "stable"
MIN_PERIODS = 3          # fewer observed periods => trend is not meaningful
CONFIDENCE_R2 = 0.5      # R^2 of the fit at/above this => "high" confidence

_DRIVER_LABELS = {
    'events': 'Fewer events',
    'demand': 'Lower demand per event',
    'acquisition': 'Fewer new buyers',
    'retention': 'Fewer returning buyers',
}


class MarketTrendCalculator:
    def __init__(self, organization, period='quarter'):
        self.organization = organization
        self.period = 'month' if period == 'month' else 'quarter'

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

        # Query A — events held per (city, period).
        events_rows = (
            Event.objects.filter(organization=org, start_date__lt=today)
            .annotate(period=trunc('start_date'))
            .values('venue__city', 'period')
            .annotate(events_held=Count('id'))
        )

        # Query B — tickets sold per (city, period). Counting the reverse
        # TicketOrder->Ticket relation is a single join; no Sum over a
        # different relation is mixed in, so there is no row inflation.
        sold_rows = (
            TicketOrder.objects.filter(
                event__organization=org,
                event__start_date__lt=today,
                is_in_person=False,
            )
            .annotate(period=trunc('event__start_date'))
            .values('event__venue__city', 'period')
            .annotate(sold=Count('tickets'))
        )

        # markets[city][period_key] = {events_held, sold, new_count, returning_count}
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

        # Query C — new vs returning buyers per (city, period), reusing the
        # earliest-order = "new" rule from RepeatCustomerCalculator.
        for city, key, new_count, ret_count in self._new_returning_by_market_period():
            cell = _bucket(city).setdefault(key, self._empty_cell())
            cell['new_count'] = new_count
            cell['returning_count'] = ret_count

        market_results = []
        for city, period_map in markets.items():
            market_results.append(self._build_market(city, period_map))

        # Most-declining first; insufficient-data markets sink to the bottom.
        def _sort_key(m):
            if m['trend'] == 'insufficient_data':
                return (1, 0.0)
            return (0, m['norm_slope_pct'])

        market_results.sort(key=_sort_key)

        summary = {
            'markets_count': len(market_results),
            'declining_count': sum(1 for m in market_results if m['trend'] == 'declining'),
            'growing_count': sum(1 for m in market_results if m['trend'] == 'growing'),
            'stable_count': sum(1 for m in market_results if m['trend'] == 'stable'),
        }
        return {'period': self.period, 'markets': market_results, 'summary': summary}

    # ----- helpers ---------------------------------------------------------

    @staticmethod
    def _empty_cell():
        return {'events_held': 0, 'sold': 0, 'new_count': 0, 'returning_count': 0}

    def _new_returning_by_market_period(self):
        """Yield (city, period_key, new_count, returning_count) tuples."""
        rows = list(
            TicketOrder.objects.filter(
                customer__organization=self.organization,
                is_in_person=False,
                event__start_date__lt=date.today(),
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
            avg = (sold / events_held) if events_held else 0.0
            total_buyers = cell['new_count'] + cell['returning_count']
            returning_pct = (
                round(cell['returning_count'] / total_buyers * 100, 1)
                if total_buyers else 0.0
            )
            periods.append({
                'period_key': k.isoformat(),
                'period_label': self._period_label(k),
                'events_held': events_held,
                'sold': sold,
                'avg_sold_per_event': round(avg, 1),
                'new_count': cell['new_count'],
                'returning_count': cell['returning_count'],
                'returning_pct': returning_pct,
            })

        result = {
            'city': city,
            'periods': periods,
            'sparkline': [p['avg_sold_per_event'] for p in periods],
            'total_sold': sum(p['sold'] for p in periods),
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

        # Only periods where events actually happened carry a turnout signal.
        observed = [p for p in periods if p['events_held'] > 0]
        if len(observed) < MIN_PERIODS:
            return result

        ys = [p['avg_sold_per_event'] for p in observed]
        slope, intercept, mean_y, r2 = _ols(ys)
        norm_slope = (slope / mean_y) if mean_y else 0.0

        result['norm_slope_pct'] = round(norm_slope * 100, 1)
        result['confidence'] = 'high' if r2 >= CONFIDENCE_R2 else 'low'
        result['trend_line'] = [round(intercept + slope * i, 1) for i in range(len(ys))]

        if norm_slope > TREND_THRESHOLD:
            result['trend'] = 'growing'
        elif norm_slope < -TREND_THRESHOLD:
            result['trend'] = 'declining'
        else:
            result['trend'] = 'stable'

        if result['trend'] == 'declining':
            self._diagnose(result, observed)

        return result

    def _diagnose(self, result, observed):
        """Attribute the decline to its dominant driver (first vs last third)."""
        n = len(observed)
        third = max(1, n // 3)
        first = observed[:third]
        last = observed[-third:]

        def _mean(rows, field):
            return sum(r[field] for r in rows) / len(rows) if rows else 0.0

        drivers = []
        for key, field in (
            ('events', 'events_held'),
            ('demand', 'avg_sold_per_event'),
            ('acquisition', 'new_count'),
            ('retention', 'returning_pct'),
        ):
            a = _mean(first, field)
            b = _mean(last, field)
            change = ((b - a) / a) if a else 0.0
            drivers.append({
                'key': key,
                'label': _DRIVER_LABELS[key],
                'change_pct': round(change * 100, 1),
                'first': round(a, 1),
                'last': round(b, 1),
            })

        result['driver_contributions'] = drivers
        # Dominant = largest negative magnitude (most responsible for the drop).
        dominant = min(drivers, key=lambda d: d['change_pct'])
        if dominant['change_pct'] >= 0:
            # Decline isn't cleanly attributable to one declining driver.
            result['dominant_driver'] = None
            result['diagnosis_text'] = (
                'Turnout in {} is trending down ~{}% per {}, but no single driver '
                'stands out — worth a closer look.'.format(
                    result['city'], abs(result['norm_slope_pct']), self.period,
                )
            )
            return

        result['dominant_driver'] = dominant['key']
        result['diagnosis_text'] = self._diagnosis_text(result, dominant)
        result['recommended_action'] = self._recommended_action(dominant['key'])

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
        elif key == 'acquisition':
            phrase = 'fewer new buyers entering the market ({}→{} per {})'.format(
                dominant['first'], dominant['last'], self.period,
            )
        else:  # retention
            phrase = 'fewer returning buyers (repeat mix fell {}%→{}%)'.format(
                dominant['first'], dominant['last'],
            )
        return 'Turnout in {} is down ~{}% per {}, driven mainly by {}.'.format(
            city, drop, self.period, phrase,
        )

    @staticmethod
    def _recommended_action(driver_key):
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
        # 'events' — programming/booking issue, informational only.
        return {
            'key': 'events',
            'label': None,
            'note': 'Turnout is slipping because fewer events are on the calendar '
                    'here. Consider booking more dates in this market.',
            'url_name': None,
            'icon': 'bi-calendar-plus',
        }


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
