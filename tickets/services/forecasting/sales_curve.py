"""
Sales curve calculation service.

Calculates historical sales curves from event ticket order data.
The sales curve shows the cumulative percentage of tickets sold
at each point in time relative to the event date.
"""
from collections import defaultdict
from datetime import datetime
from typing import Union

from django.db.models import Count
from django.utils import timezone

from tickets.models import Event
from .models import SalesCurve, DailySales


class SalesCurveCalculator:
    """
    Calculates sales curves from historical event data.

    A sales curve represents the cumulative percentage of tickets sold
    over time, measured in days before the event date.
    """

    def calculate_days_before(
        self,
        order_date: Union[datetime, 'datetime'],
        event_date: Union[datetime, 'datetime']
    ) -> int:
        """
        Calculate the number of days before the event an order was placed.

        Args:
            order_date: When the order was placed
            event_date: When the event occurs

        Returns:
            Number of days before event (negative if after event)
        """
        # Ensure we're comparing dates, not datetimes
        if hasattr(order_date, 'date'):
            order_day = order_date.date()
        else:
            order_day = order_date

        if hasattr(event_date, 'date'):
            event_day = event_date.date()
        else:
            event_day = event_date

        delta = event_day - order_day
        return delta.days

    def aggregate_daily_sales(self, event: Event) -> dict:
        """
        Aggregate ticket sales by days-before-event.

        Args:
            event: The Event to analyze

        Returns:
            Dict mapping days_before_event -> ticket_count
        """
        # Get all orders for this event with their tickets
        orders = event.ticket_orders.prefetch_related('tickets').all()

        if not orders.exists():
            return {}

        daily_counts = defaultdict(int)

        for order in orders:
            days_before = self.calculate_days_before(
                order.order_date,
                event.event_date
            )
            # Count tickets in this order
            ticket_count = order.tickets.count()
            daily_counts[days_before] += ticket_count

        return dict(daily_counts)

    def normalize_cumulative(self, daily_sales: dict, capacity: int = None) -> dict:
        """
        Convert daily ticket counts to cumulative percentages.

        The cumulative percentage at each day represents the total
        percentage of tickets sold from the start through that day.
        When capacity is provided, percentages are % of capacity (sell-through);
        otherwise % of actual sales (normalized to 100% at event date).

        Args:
            daily_sales: Dict of days_before -> ticket_count
            capacity: Optional total capacity; when set, normalize by capacity

        Returns:
            Dict of days_before -> cumulative_percentage
        """
        if not daily_sales:
            return {}

        total_tickets = sum(daily_sales.values())
        if total_tickets == 0:
            return {}

        denominator = capacity if capacity and capacity > 0 else total_tickets

        # Sort days in descending order (furthest from event first)
        sorted_days = sorted(daily_sales.keys(), reverse=True)

        cumulative = {}
        running_total = 0

        for day in sorted_days:
            running_total += daily_sales[day]
            cumulative[day] = (running_total / denominator) * 100

        return cumulative

    def get_event_sales_curve(self, event: Event, use_capacity: bool = False) -> SalesCurve:
        """
        Generate a complete sales curve for an event.

        Args:
            event: The Event to analyze
            use_capacity: If True and event has capacity, normalize by capacity (sell-through)

        Returns:
            SalesCurve object with normalized cumulative percentages
        """
        daily_sales = self.aggregate_daily_sales(event)

        if not daily_sales:
            return SalesCurve(points={}, total_tickets=0)

        total_tickets = sum(daily_sales.values())
        capacity = event.capacity if use_capacity and event.capacity else None
        cumulative = self.normalize_cumulative(daily_sales, capacity=capacity)

        return SalesCurve(
            points=cumulative,
            total_tickets=total_tickets
        )

    def get_event_cumulative_tickets(self, event: Event) -> dict:
        """
        Get cumulative ticket count by days-before-event (no percentage).

        Args:
            event: The Event to analyze

        Returns:
            Dict mapping days_before_event -> cumulative_ticket_count (sparse)
        """
        daily_sales = self.aggregate_daily_sales(event)
        if not daily_sales:
            return {}
        sorted_days = sorted(daily_sales.keys(), reverse=True)
        cumulative = {}
        running_total = 0
        for day in sorted_days:
            running_total += daily_sales[day]
            cumulative[day] = running_total
        return cumulative

    def get_detailed_sales_data(self, event: Event) -> list:
        """
        Get detailed daily sales data for an event.

        Returns a list of DailySales objects with complete information
        about each day's sales.

        Args:
            event: The Event to analyze

        Returns:
            List of DailySales objects, sorted by days_before_event descending
        """
        daily_sales = self.aggregate_daily_sales(event)

        if not daily_sales:
            return []

        total_tickets = sum(daily_sales.values())
        sorted_days = sorted(daily_sales.keys(), reverse=True)

        result = []
        running_total = 0

        for day in sorted_days:
            tickets_today = daily_sales[day]
            running_total += tickets_today
            cumulative_pct = (running_total / total_tickets) * 100 if total_tickets > 0 else 0

            result.append(DailySales(
                days_before_event=day,
                tickets_sold=tickets_today,
                cumulative_tickets=running_total,
                cumulative_percent=cumulative_pct
            ))

        return result
