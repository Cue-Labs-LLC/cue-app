"""
Behavior profile classification for customer purchase patterns.
"""
from decimal import Decimal

from django.db.models import Count, Max, Min
from django.db.models.functions import TruncDate
from django.utils import timezone

from tickets.models import Customer

CHUNK_SIZE = 2000
BEHAVIOR_UPDATE_FIELDS = [
    "behavior_profile",
    "behavior_profile_reason",
    "days_since_last_order",
    "avg_days_between_orders",
    "days_to_second_order",
]

BEHAVIOR_PROFILE_ORDER = [
    "Fast Repeat",
    "Steady Repeat",
    "Occasional Repeat",
    "High-Value Occasional",
    "Slowing Down",
    "Inactive Repeat",
    "First-Time Recent",
    "Dormant",
]

BEHAVIOR_PROFILE_BADGE_COLORS = {
    "Fast Repeat": "success",
    "Steady Repeat": "primary",
    "Occasional Repeat": "info",
    "High-Value Occasional": "danger",
    "Slowing Down": "warning",
    "Inactive Repeat": "secondary",
    "First-Time Recent": "primary",
    "Dormant": "secondary",
}

BEHAVIOR_PROFILE_DESCRIPTIONS = {
    "Fast Repeat": "Buys again quickly and is currently active.",
    "Steady Repeat": "Comes back on a dependable repeat cadence.",
    "Occasional Repeat": "Returns, but with longer gaps between purchases.",
    "High-Value Occasional": "Buys infrequently, but contributes outsized value when active.",
    "Slowing Down": "Used to buy on a faster cadence and is now behind that pattern.",
    "Inactive Repeat": "Has repeat purchase history but has been inactive for too long.",
    "First-Time Recent": "A recent first purchase with no repeat behavior yet.",
    "Dormant": "No meaningful recent buying behavior yet.",
}


class CustomerBehaviorProfiler:
    """Compute customer purchase-pattern profiles for an organization."""

    def __init__(self, organization):
        self.organization = organization

    def _base_queryset(self):
        return (
            Customer.objects.filter(organization=self.organization)
            .exclude(email__endswith='@placeholder.local')
            .values('id', 'lifetime_value', 'last_order_date')
        )

    def _customer_order_stats(self):
        from tickets.models import TicketOrder

        order_rows = (
            TicketOrder.objects.filter(
                customer__organization=self.organization,
                refunded_at__isnull=True,
            )
            .exclude(customer__email__endswith='@placeholder.local')
            .annotate(order_day=TruncDate('order_date'))
            .values('customer_id')
            .annotate(
                order_count=Count('id'),
                first_order_date=Min('order_day'),
                latest_order_date=Max('order_day'),
            )
        )
        stats = {
            row['customer_id']: {
                'order_count': row['order_count'] or 0,
                'first_order_date': row['first_order_date'],
                'latest_order_date': row['latest_order_date'],
            }
            for row in order_rows
        }

        if not stats:
            return stats

        gap_rows = (
            TicketOrder.objects.filter(
                customer__organization=self.organization,
                refunded_at__isnull=True,
                customer_id__in=stats.keys(),
            )
            .exclude(customer__email__endswith='@placeholder.local')
            .annotate(order_day=TruncDate('order_date'))
            .values('customer_id', 'order_day')
            .annotate(order_count=Count('id'))
            .order_by('customer_id', 'order_day')
        )

        for customer_id, values in self._build_gap_metrics(gap_rows).items():
            stats.setdefault(customer_id, {}).update(values)

        return stats

    def _build_gap_metrics(self, rows):
        metrics = {}
        current_customer_id = None
        dates = []

        def flush():
            if current_customer_id is None:
                return
            metrics[current_customer_id] = self._metrics_from_dates(dates)

        for row in rows.iterator(chunk_size=CHUNK_SIZE):
            customer_id = row['customer_id']
            order_day = row['order_day']
            if customer_id != current_customer_id:
                flush()
                current_customer_id = customer_id
                dates = []
            if not dates or dates[-1] != order_day:
                dates.append(order_day)
        flush()
        return metrics

    def _metrics_from_dates(self, dates):
        if not dates:
            return {
                'avg_days_between_orders': None,
                'days_to_second_order': None,
            }
        if len(dates) == 1:
            return {
                'avg_days_between_orders': None,
                'days_to_second_order': None,
            }

        gaps = [
            (dates[idx] - dates[idx - 1]).days
            for idx in range(1, len(dates))
        ]
        avg_gap = int(round(sum(gaps) / len(gaps))) if gaps else None
        days_to_second_order = (dates[1] - dates[0]).days if len(dates) > 1 else None
        return {
            'avg_days_between_orders': avg_gap,
            'days_to_second_order': days_to_second_order,
        }

    def _monetary_breakpoints(self):
        spends = [
            float(row['lifetime_value'] or 0)
            for row in self._base_queryset().iterator(chunk_size=CHUNK_SIZE)
        ]
        if not spends:
            return None
        spends.sort()
        index = int(len(spends) * 0.8)
        index = min(index, len(spends) - 1)
        return Decimal(str(spends[index]))

    def _classify(self, now, customer_row, order_stats, high_value_cutoff):
        order_count = order_stats.get('order_count') or 0
        last_order_date = (
            customer_row['last_order_date']
            or order_stats.get('latest_order_date')
        )
        days_since_last_order = (
            (now - last_order_date).days if last_order_date else None
        )
        avg_gap = order_stats.get('avg_days_between_orders')
        days_to_second_order = order_stats.get('days_to_second_order')
        lifetime_value = customer_row['lifetime_value'] or Decimal('0.00')

        if order_count <= 0:
            return "Dormant", "No completed orders yet.", days_since_last_order, avg_gap, days_to_second_order

        if order_count == 1:
            if days_since_last_order is not None and days_since_last_order <= 45:
                return (
                    "First-Time Recent",
                    "Made a first purchase recently and has not had time to repeat yet.",
                    days_since_last_order,
                    avg_gap,
                    days_to_second_order,
                )
            return (
                "Dormant",
                "Only one purchase so far and no recent repeat activity.",
                days_since_last_order,
                avg_gap,
                days_to_second_order,
            )

        if days_since_last_order is not None and days_since_last_order >= 180:
            return (
                "Inactive Repeat",
                "Has repeat purchase history but has been inactive for a long stretch.",
                days_since_last_order,
                avg_gap,
                days_to_second_order,
            )

        slowdown_threshold = int(round((avg_gap or 0) * 1.75)) if avg_gap else None
        if (
            order_count >= 3
            and avg_gap
            and days_since_last_order is not None
            and days_since_last_order >= max(60, slowdown_threshold or 0)
        ):
            return (
                "Slowing Down",
                "Current inactivity is materially longer than the customer's usual buying cadence.",
                days_since_last_order,
                avg_gap,
                days_to_second_order,
            )

        if avg_gap is not None and avg_gap <= 30 and days_since_last_order is not None and days_since_last_order <= 45:
            return (
                "Fast Repeat",
                "Returns quickly after each purchase and is currently active.",
                days_since_last_order,
                avg_gap,
                days_to_second_order,
            )

        if avg_gap is not None and avg_gap <= 75 and days_since_last_order is not None and days_since_last_order <= 90:
            return (
                "Steady Repeat",
                "Shows a consistent repeat cadence without large gaps.",
                days_since_last_order,
                avg_gap,
                days_to_second_order,
            )

        if lifetime_value >= (high_value_cutoff or Decimal('0.00')):
            return (
                "High-Value Occasional",
                "Buys less often, but contributes high value when they do purchase.",
                days_since_last_order,
                avg_gap,
                days_to_second_order,
            )

        return (
            "Occasional Repeat",
            "Returns over time, but with longer gaps between purchases.",
            days_since_last_order,
            avg_gap,
            days_to_second_order,
        )

    def calculate_all(self):
        now = timezone.now().date()
        order_stats = self._customer_order_stats()
        high_value_cutoff = self._monetary_breakpoints()

        chunk = []
        for row in self._base_queryset().iterator(chunk_size=CHUNK_SIZE):
            profile, reason, days_since_last_order, avg_gap, days_to_second_order = self._classify(
                now,
                row,
                order_stats.get(row['id'], {}),
                high_value_cutoff,
            )
            chunk.append(Customer(
                id=row['id'],
                behavior_profile=profile,
                behavior_profile_reason=reason[:255],
                days_since_last_order=days_since_last_order,
                avg_days_between_orders=avg_gap,
                days_to_second_order=days_to_second_order,
            ))
            if len(chunk) >= CHUNK_SIZE:
                Customer.objects.bulk_update(chunk, BEHAVIOR_UPDATE_FIELDS)
                chunk = []
        if chunk:
            Customer.objects.bulk_update(chunk, BEHAVIOR_UPDATE_FIELDS)
