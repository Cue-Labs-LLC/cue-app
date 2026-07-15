"""
Audience growth analysis.

Tracks how an organization's customer base has grown over time. A customer is
considered "acquired" on the date of their first ticket order; when scoped to a
market, acquisition is the date of their first order on an event in that market
(so a customer active in several markets is counted in each of them).

Returns a monthly series of new customers plus the cumulative running total,
suitable for a combo (bar + line) chart.
"""
import pandas as pd
from django.db.models import Min
from django.utils import timezone

from tickets.models import TicketOrder


class AudienceGrowthCalculator:
    """Monthly new-customer counts and cumulative audience size for an org.

    market_id: restrict to events in a single market (customer acquired on their
        first order within that market).
    no_market: restrict to orders on events with no market assigned.
    Passing neither yields org-wide growth across all customers with orders.
    """

    def __init__(self, organization, market_id=None, no_market=False):
        self.organization = organization
        self.market_id = market_id
        self.no_market = no_market

    def _empty(self):
        return {
            'series': [],
            'summary': {
                'total_customers': 0,
                'new_last_12_months': 0,
                'peak_month': '',
                'first_month': '',
            },
        }

    def calculate(self):
        orders = TicketOrder.objects.filter(
            customer__organization=self.organization,
            event__organization=self.organization,
        )
        if self.no_market:
            orders = orders.filter(event__market__isnull=True)
        elif self.market_id:
            orders = orders.filter(event__market_id=self.market_id)

        # One row per customer = their earliest order date within scope. The
        # (customer, order_date) composite index covers this aggregation.
        rows = list(orders.values('customer').annotate(first_order=Min('order_date')))
        if not rows:
            return self._empty()

        df = pd.DataFrame(rows)
        first_order = pd.to_datetime(df['first_order'])
        # order_date is tz-aware in prod (USE_TZ); bucket by local wall-clock
        # month so periods line up with how organizers think about calendar months.
        if getattr(first_order.dt, 'tz', None) is not None:
            first_order = first_order.dt.tz_convert(
                timezone.get_current_timezone()
            ).dt.tz_localize(None)
        months = first_order.dt.to_period('M')

        monthly = months.groupby(months).size()
        # Fill gaps so months with zero new customers still advance the line.
        full_range = pd.period_range(monthly.index.min(), monthly.index.max(), freq='M')
        monthly = monthly.reindex(full_range, fill_value=0)
        cumulative = monthly.cumsum()

        series = [
            {
                'month': str(period),
                'new_customers': int(new),
                'cumulative': int(total),
            }
            for period, new, total in zip(monthly.index, monthly.values, cumulative.values)
        ]

        cutoff = pd.Period(timezone.localdate(), freq='M') - 11  # trailing 12 months
        peak_period = monthly.idxmax()
        summary = {
            'total_customers': int(cumulative.iloc[-1]),
            'new_last_12_months': int(monthly[monthly.index >= cutoff].sum()),
            'peak_month': str(peak_period),
            'first_month': str(monthly.index.min()),
        }
        return {'series': series, 'summary': summary}
