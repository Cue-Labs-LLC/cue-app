"""
Historical data aggregation service for baseline curve generation.

Aggregates sales curves from multiple historical events to create
baseline projections for new events.
"""
from collections import defaultdict
from typing import Optional

from django.db.models import QuerySet, Count
from django.utils import timezone

from tickets.models import Event, Venue
from .sales_curve import SalesCurveCalculator
from .models import SalesCurve, BaselineCurveData


class HistoricalAggregator:
    """
    Aggregates historical event data to create baseline sales curves.

    The aggregator filters events by segment (global, city, venue) and
    combines their sales curves to create an average baseline curve.
    """

    def __init__(self, min_events: int = 1, organization=None):
        """
        Initialize the aggregator.

        Args:
            min_events: Minimum number of events required for a segment
                       to be considered valid. Falls back to broader
                       segment if threshold not met.
            organization: Optional organization to scope events to (current org for forecast).
        """
        self.min_events = min_events
        self.organization = organization
        self.curve_calculator = SalesCurveCalculator()

    def get_events_for_segment(
        self,
        city: Optional[str] = None,
        venue: Optional[Venue] = None
    ) -> QuerySet:
        """
        Get historical events for a given segment.

        Returns only past events with at least one ticket order.

        Args:
            city: Filter by venue city name
            venue: Filter by specific venue

        Returns:
            QuerySet of Event objects
        """
        # Start with past events that have orders
        events = Event.objects.filter(
            start_date__lt=timezone.now().date()
        ).annotate(
            order_count=Count('ticket_orders')
        ).filter(
            order_count__gt=0
        )

        # Apply venue filter (most specific)
        if venue is not None:
            events = events.filter(venue=venue)
        # Apply city filter
        elif city is not None:
            events = events.filter(venue__city=city)

        if self.organization is not None:
            events = events.filter(organization=self.organization)

        return events.order_by('-start_date')

    def aggregate_curves(self, events: QuerySet, use_capacity: bool = True) -> BaselineCurveData:
        """
        Aggregate sales curves from multiple events into a baseline.

        Takes the average cumulative percentage at each day across
        all events in the queryset. When use_capacity is True, only
        events with capacity are included and curves are % of capacity
        (sell-through); if too few have capacity, falls back to % of sales.

        Args:
            events: QuerySet of Event objects to aggregate
            use_capacity: If True, use capacity-based curves (sell-through)

        Returns:
            BaselineCurveData with averaged curve points
        """
        if not events.exists():
            return BaselineCurveData(
                points={},
                events_count=0
            )

        # Collect all individual curves, capacities, and cumulative ticket curves
        curves = []
        capacities = []
        ticket_curves = []
        for event in events:
            if use_capacity and not event.capacity:
                continue
            curve = self.curve_calculator.get_event_sales_curve(
                event, use_capacity=use_capacity
            )
            if curve.points:  # Only include events with sales data
                curves.append(curve)
                if use_capacity and event.capacity:
                    capacities.append(event.capacity)
                cum_tickets = self.curve_calculator.get_event_cumulative_tickets(event)
                if cum_tickets:
                    ticket_curves.append(cum_tickets)

        # If too few events with capacity, fall back to no-capacity mode
        if len(curves) < self.min_events and use_capacity:
            return self.aggregate_curves(events, use_capacity=False)

        if not curves:
            return BaselineCurveData(
                points={},
                events_count=0
            )

        # Find all unique days across all curves
        all_days = set()
        for curve in curves:
            all_days.update(curve.points.keys())

        # Average the values at each day
        averaged_points = {}
        for day in sorted(all_days, reverse=True):
            values = []
            for curve in curves:
                # Use interpolation for curves that don't have this exact day
                if day in curve.points:
                    values.append(curve.points[day])
                else:
                    # Interpolate value for this day
                    values.append(curve.interpolate(day))

            averaged_points[day] = sum(values) / len(values)

        # Average cumulative ticket counts per day (for ticket-count baseline)
        points_tickets = None
        if ticket_curves:
            all_days_tickets = set()
            for tc in ticket_curves:
                all_days_tickets.update(tc.keys())
            averaged_tickets = {}
            for day in sorted(all_days_tickets, reverse=True):
                values = []
                for tc in ticket_curves:
                    sc = SalesCurve(points=tc, total_tickets=0)
                    values.append(sc.interpolate(day))
                averaged_tickets[day] = sum(values) / len(values)
            points_tickets = averaged_tickets

        max_historical_capacity = max(capacities) if capacities else None
        return BaselineCurveData(
            points=averaged_points,
            events_count=len(curves),
            max_historical_capacity=max_historical_capacity,
            points_tickets=points_tickets,
        )

    def _filter_events_by_sales_length(
        self, events: QuerySet, reference_days_before: int
    ) -> QuerySet:
        """
        Filter events to those whose sales length is in a band around reference.

        Sales length for an event = max(days_before) in that event's curve.
        Band is [0.5 * reference_days_before, 2 * reference_days_before].
        If fewer than min_events pass, return the original queryset (unfiltered).
        """
        if reference_days_before <= 0:
            return events
        low = max(0, int(0.5 * reference_days_before))
        high = int(2 * reference_days_before)
        event_ids = []
        for event in events:
            curve = self.curve_calculator.get_event_sales_curve(event)
            sales_length = max(curve.points.keys()) if curve.points else 0
            if low <= sales_length <= high:
                event_ids.append(event.id)
        if len(event_ids) >= self.min_events:
            return events.filter(id__in=event_ids)
        return events

    def get_baseline_curve(
        self,
        city: Optional[str] = None,
        venue: Optional[Venue] = None,
        reference_days_before: Optional[int] = None,
    ) -> BaselineCurveData:
        """
        Get the best available baseline curve for a segment.

        Attempts to get the most specific segment with sufficient events,
        falling back to broader segments if necessary.

        Fallback order: venue -> city -> global

        Args:
            city: Preferred city segment
            venue: Preferred venue segment (most specific)
            reference_days_before: If set, filter to events whose sales length
                (max days_before in curve) is in [0.5*ref, 2*ref]. If fewer
                than min_events pass, use unfiltered events.

        Returns:
            BaselineCurveData with segment information
        """
        # Try venue-specific first (most specific)
        if venue is not None:
            events = self.get_events_for_segment(venue=venue)
            if events.count() >= self.min_events:
                if reference_days_before is not None:
                    events = self._filter_events_by_sales_length(
                        events, reference_days_before
                    )
                baseline = self.aggregate_curves(events)
                baseline.segment_type = 'venue'
                baseline.segment_id = str(venue.id)
                return baseline

            # Fall back to city (venue's city)
            city = venue.city

        # Try city-specific
        if city is not None:
            events = self.get_events_for_segment(city=city)
            if events.count() >= self.min_events:
                if reference_days_before is not None:
                    events = self._filter_events_by_sales_length(
                        events, reference_days_before
                    )
                baseline = self.aggregate_curves(events)
                baseline.segment_type = 'city'
                baseline.segment_id = city
                return baseline

        # Fall back to global
        events = self.get_events_for_segment()
        if reference_days_before is not None:
            events = self._filter_events_by_sales_length(
                events, reference_days_before
            )
        baseline = self.aggregate_curves(events)
        baseline.segment_type = 'global'
        baseline.segment_id = None
        return baseline

    def get_events_used_for_baseline(
        self,
        city: Optional[str] = None,
        venue: Optional[Venue] = None,
        reference_days_before: Optional[int] = None,
    ) -> QuerySet:
        """
        Return the same Event queryset used to build the baseline curve.

        Mirrors the fallback logic in get_baseline_curve (venue -> city -> global)
        and applies the same reference_days_before filter. Use this to list
        historical events that contributed to the baseline.

        Args:
            city: Preferred city segment (used when falling back from venue).
            venue: Preferred venue segment (most specific).
            reference_days_before: If set, filter to events whose sales length
                is in [0.5*ref, 2*ref]. If fewer than min_events pass,
                returns unfiltered events.

        Returns:
            QuerySet of Event objects, ordered by event_date descending.
        """
        # Try venue-specific first (most specific)
        if venue is not None:
            events = self.get_events_for_segment(venue=venue)
            if events.count() >= self.min_events:
                if reference_days_before is not None:
                    events = self._filter_events_by_sales_length(
                        events, reference_days_before
                    )
                return events.order_by('-start_date')
            city = venue.city

        # Try city-specific
        if city is not None:
            events = self.get_events_for_segment(city=city)
            if events.count() >= self.min_events:
                if reference_days_before is not None:
                    events = self._filter_events_by_sales_length(
                        events, reference_days_before
                    )
                return events.order_by('-start_date')

        # Fall back to global
        events = self.get_events_for_segment()
        if reference_days_before is not None:
            events = self._filter_events_by_sales_length(
                events, reference_days_before
            )
        return events.order_by('-start_date')

    def get_available_segments(self) -> dict:
        """
        Get information about available segments and their event counts.

        Useful for UI to show which segments have enough data.

        Returns:
            Dict with segment info:
            {
                'global': {'count': N, 'valid': bool},
                'cities': {'City Name': {'count': N, 'valid': bool}, ...},
                'venues': {'Venue ID': {'name': str, 'count': N, 'valid': bool}, ...}
            }
        """
        result = {
            'global': {'count': 0, 'valid': False},
            'cities': {},
            'venues': {}
        }

        # Get IDs of all past events with orders (using existing method)
        base_events = self.get_events_for_segment()
        event_ids = list(base_events.values_list('id', flat=True))

        result['global']['count'] = len(event_ids)
        result['global']['valid'] = result['global']['count'] >= self.min_events

        if not event_ids:
            return result

        # Use a fresh queryset filtered by the IDs for grouping
        # This avoids issues with the existing order_count annotation
        events_for_grouping = Event.objects.filter(id__in=event_ids)

        # Group by city
        city_counts = events_for_grouping.values('venue__city').annotate(
            count=Count('id')
        )
        for item in city_counts:
            city = item['venue__city']
            count = item['count']
            result['cities'][city] = {
                'count': count,
                'valid': count >= self.min_events
            }

        # Group by venue
        venue_counts = events_for_grouping.values(
            'venue__id', 'venue__name', 'venue__city'
        ).annotate(
            count=Count('id')
        )
        for item in venue_counts:
            venue_id = str(item['venue__id'])
            result['venues'][venue_id] = {
                'name': f"{item['venue__name']}, {item['venue__city']}",
                'count': item['count'],
                'valid': item['count'] >= self.min_events
            }

        return result
