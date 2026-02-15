"""
Forecast generation service.

Generates ticket sales projections for upcoming events based on
historical baseline curves.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional, List

from django.db.models import Count
from django.utils import timezone

from tickets.models import Event
from .sales_curve import SalesCurveCalculator
from .historical_aggregator import HistoricalAggregator
from .models import SalesCurve, BaselineCurveData, PaceMetrics


@dataclass
class ProjectionPoint:
    """A single point in a projection curve."""
    days_before_event: int
    expected_tickets: int
    expected_percent: float

    def to_dict(self) -> dict:
        return {
            'days_before_event': self.days_before_event,
            'expected_tickets': self.expected_tickets,
            'expected_percent': self.expected_percent,
        }


@dataclass
class Projection:
    """Complete projection for an event."""
    event: Event
    capacity: int
    baseline_curve: SalesCurve
    baseline_used: BaselineCurveData
    projected_curve: List[ProjectionPoint] = field(default_factory=list)
    has_sufficient_data: bool = True

    def to_dict(self) -> dict:
        return {
            'event_id': str(self.event.id),
            'event_name': self.event.name,
            'capacity': self.capacity,
            'has_sufficient_data': self.has_sufficient_data,
            'baseline_segment': self.baseline_used.segment_type,
            'baseline_events_count': self.baseline_used.events_count,
            'projected_curve': [p.to_dict() for p in self.projected_curve],
        }


class ForecastGenerator:
    """
    Generates sales projections for upcoming events.

    Uses historical baseline curves to project expected sales and
    calculates pace metrics comparing actual vs expected performance.
    """

    def __init__(
        self,
        min_events: int = 1,
        on_track_threshold: float = 0.10
    ):
        """
        Initialize the forecast generator.

        Args:
            min_events: Minimum events required for valid baseline
            on_track_threshold: Percentage threshold for 'on_track' status
                              (e.g., 0.10 = within 10% is on track)
        """
        self.min_events = min_events
        self.on_track_threshold = on_track_threshold
        self.curve_calculator = SalesCurveCalculator()
        self.aggregator = HistoricalAggregator(min_events=min_events)

    def generate_projection(
        self,
        event: Event,
        capacity: int
    ) -> Projection:
        """
        Generate a sales projection for an event.

        Args:
            event: The Event to project
            capacity: Expected total ticket capacity

        Returns:
            Projection with expected sales curve
        """
        # Get baseline curve (will try venue -> city -> global)
        baseline = self.aggregator.get_baseline_curve(venue=event.venue)

        if baseline.events_count == 0:
            return Projection(
                event=event,
                capacity=capacity,
                baseline_curve=SalesCurve(),
                baseline_used=baseline,
                projected_curve=[],
                has_sufficient_data=False
            )

        # Create projection curve from baseline
        projected_curve = []

        # Generate projection points for key days
        # Start from furthest out in baseline and go to day 0
        if baseline.points:
            sorted_days = sorted(baseline.points.keys(), reverse=True)

            for day in sorted_days:
                expected_pct = baseline.points[day]
                expected_tickets = int(capacity * expected_pct / 100)

                projected_curve.append(ProjectionPoint(
                    days_before_event=day,
                    expected_tickets=expected_tickets,
                    expected_percent=expected_pct
                ))

        # Convert baseline to SalesCurve for lookups
        baseline_sales_curve = SalesCurve(
            points=baseline.points,
            total_tickets=capacity
        )

        return Projection(
            event=event,
            capacity=capacity,
            baseline_curve=baseline_sales_curve,
            baseline_used=baseline,
            projected_curve=projected_curve,
            has_sufficient_data=baseline.events_count >= self.min_events
        )

    def calculate_pace(
        self,
        event: Event,
        capacity: int
    ) -> PaceMetrics:
        """
        Calculate pace metrics for an event.

        Compares actual sales to expected based on baseline curve.

        Args:
            event: The Event to analyze
            capacity: Expected total ticket capacity

        Returns:
            PaceMetrics with current pace and projections
        """
        # Get current sales
        current_curve = self.curve_calculator.get_event_sales_curve(event)
        current_sold = current_curve.total_tickets
        current_percent = (current_sold / capacity * 100) if capacity > 0 else 0

        # Calculate days to event
        now = timezone.now()
        days_to_event = (event.start_date - now.date()).days

        # Get baseline expectation for this point
        baseline = self.aggregator.get_baseline_curve(venue=event.venue)

        if baseline.events_count == 0:
            # No historical data - can't calculate pace
            return PaceMetrics(
                days_to_event=days_to_event,
                current_sold=current_sold,
                current_percent=current_percent,
                expected_percent=0.0,
                pace_delta=0.0,
                projected_total=current_sold,
                sellout_date=None,
                status='on_track'
            )

        expected_percent = baseline.lookup(days_to_event)

        # Calculate pace delta (positive = ahead, negative = behind)
        if expected_percent > 0:
            pace_delta = (current_percent - expected_percent) / expected_percent
        else:
            pace_delta = 0.0 if current_percent == 0 else 1.0  # Any sales when 0 expected = ahead

        # Determine status
        if abs(pace_delta) <= self.on_track_threshold:
            status = 'on_track'
        elif pace_delta > 0:
            status = 'ahead'
        else:
            status = 'behind'

        # Project total sales
        projected_total = self._project_total_sales(
            current_percent=current_percent,
            expected_percent=expected_percent,
            capacity=capacity,
            days_to_event=days_to_event
        )

        # Estimate sell-out date
        sellout_date = self._estimate_sellout_date(
            current_sold=current_sold,
            current_percent=current_percent,
            capacity=capacity,
            days_to_event=days_to_event,
            projected_total=projected_total,
            event_date=event.start_date
        )

        return PaceMetrics(
            days_to_event=days_to_event,
            current_sold=current_sold,
            current_percent=round(current_percent, 2),
            expected_percent=round(expected_percent, 2),
            pace_delta=round(pace_delta, 4),
            projected_total=projected_total,
            sellout_date=sellout_date,
            status=status
        )

    def _project_total_sales(
        self,
        current_percent: float,
        expected_percent: float,
        capacity: int,
        days_to_event: int
    ) -> int:
        """
        Project total sales based on current pace.

        Uses the ratio of current vs expected to scale final projection.
        """
        if expected_percent <= 0:
            # No baseline at this point, use current as projection
            return min(capacity, int(current_percent * capacity / 100))

        if current_percent <= 0:
            # No sales yet, project based on baseline
            return capacity  # Assume baseline sells out

        # Calculate pace ratio
        pace_ratio = current_percent / expected_percent

        # Assume baseline reaches 100%, apply pace ratio
        projected_percent = min(100.0, 100.0 * pace_ratio)

        return min(capacity, int(projected_percent * capacity / 100))

    def _estimate_sellout_date(
        self,
        current_sold: int,
        current_percent: float,
        capacity: int,
        days_to_event: int,
        projected_total: int,
        event_date: date
    ) -> Optional[str]:
        """
        Estimate when event will sell out.

        Returns None if not projected to sell out before event.
        """
        if current_sold >= capacity:
            # Already sold out
            return date.today().isoformat()

        if projected_total < capacity:
            # Not projected to sell out
            return None

        if current_sold == 0:
            # No sales yet, can't estimate
            return None

        # Calculate remaining capacity and days
        remaining = capacity - current_sold
        remaining_percent = 100.0 - current_percent

        if remaining_percent <= 0:
            return date.today().isoformat()

        # Estimate days needed based on current rate
        # Assume sales continue at current daily rate
        if days_to_event <= 0:
            return None

        # Simple linear projection
        # Current percent achieved in (total_days - days_to_event) days
        # But we don't know when sales started, so use days_to_event as reference
        percent_per_day = current_percent / max(1, 30 - days_to_event) if days_to_event < 30 else current_percent / 30

        if percent_per_day <= 0:
            return None

        days_to_sellout = int(remaining_percent / percent_per_day)

        sellout_date = date.today() + timedelta(days=days_to_sellout)

        # Only return if before event
        if sellout_date <= event_date:
            return sellout_date.isoformat()

        return None

    def get_forecast_summary(
        self,
        event: Event,
        capacity: int
    ) -> dict:
        """
        Get a complete forecast summary for an event.

        Combines projection and pace metrics into a single response.

        Args:
            event: The Event to forecast
            capacity: Expected total ticket capacity

        Returns:
            Dictionary with complete forecast data
        """
        projection = self.generate_projection(event, capacity)
        pace = self.calculate_pace(event, capacity)

        return {
            'event': {
                'id': str(event.id),
                'name': event.name,
                'date': event.start_date.isoformat(),
                'venue': str(event.venue),
            },
            'capacity': capacity,
            'has_sufficient_data': projection.has_sufficient_data,
            'baseline': {
                'segment_type': projection.baseline_used.segment_type,
                'segment_id': projection.baseline_used.segment_id,
                'events_count': projection.baseline_used.events_count,
            },
            'current_state': {
                'tickets_sold': pace.current_sold,
                'percent_sold': pace.current_percent,
                'days_to_event': pace.days_to_event,
            },
            'pace': {
                'expected_percent': pace.expected_percent,
                'pace_delta': pace.pace_delta,
                'pace_delta_display': f"{pace.pace_delta:+.0%}",
                'status': pace.status,
            },
            'projection': {
                'projected_total': pace.projected_total,
                'projected_percent': round(pace.projected_total / capacity * 100, 1) if capacity > 0 else 0,
                'sellout_date': pace.sellout_date,
            },
            'curve': [p.to_dict() for p in projection.projected_curve],
        }
