"""
Data structures for ticket sales forecasting.

These are lightweight dataclasses used for in-memory calculations,
not Django ORM models. Django models for persistence are defined separately.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DailySales:
    """Represents sales data for a single day relative to event."""
    days_before_event: int
    tickets_sold: int
    cumulative_tickets: int
    cumulative_percent: float

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            'days_before_event': self.days_before_event,
            'tickets_sold': self.tickets_sold,
            'cumulative_tickets': self.cumulative_tickets,
            'cumulative_percent': self.cumulative_percent,
        }


@dataclass
class SalesCurve:
    """
    Represents a normalized sales curve for an event.

    The curve shows cumulative percentage of tickets sold at each point
    in time relative to the event date.

    Attributes:
        points: Dict mapping days_before_event -> cumulative_percent
        total_tickets: Total number of tickets sold for this event
    """
    points: dict = field(default_factory=dict)  # days_before -> cumulative %
    total_tickets: int = 0

    def lookup(self, days_before: int) -> float:
        """
        Look up the expected cumulative percentage for a given day.

        If the exact day is not in the curve, returns:
        - 0.0 if before any sales
        - Last known value (closest to event) if after event (negative days)
        - The exact value if the day exists in points
        """
        if not self.points:
            return 0.0

        if days_before in self.points:
            return self.points[days_before]

        # If negative days (after event), return value at day closest to event (smallest day)
        if days_before < 0:
            sorted_days = sorted(self.points.keys())
            if sorted_days:
                return self.points[sorted_days[0]]
            return 0.0

        # Get sorted days (descending - further from event first)
        sorted_days = sorted(self.points.keys(), reverse=True)

        # If before first sale, return 0
        if days_before > sorted_days[0]:
            return 0.0

        # If after last data point but before event, return last value
        if days_before < sorted_days[-1]:
            return self.points[sorted_days[-1]]

        # Return value at the day (or use interpolate for between days)
        return self.interpolate(days_before)

    def interpolate(self, days_before: int) -> float:
        """
        Linearly interpolate value for a day between known points.

        Args:
            days_before: The day to interpolate for

        Returns:
            Interpolated cumulative percentage
        """
        if not self.points:
            return 0.0

        if days_before in self.points:
            return self.points[days_before]

        # Get sorted days (descending - further from event first)
        sorted_days = sorted(self.points.keys(), reverse=True)

        # Find the two points to interpolate between
        upper_day = None
        lower_day = None

        for day in sorted_days:
            if day > days_before:
                upper_day = day
            elif day < days_before:
                lower_day = day
                break

        # Handle edge cases
        if upper_day is None:
            # Before any sales
            return 0.0
        if lower_day is None:
            # After all data but before event
            return self.points[sorted_days[-1]]

        # Linear interpolation
        upper_val = self.points[upper_day]
        lower_val = self.points[lower_day]

        # Calculate position between points (0 = at upper, 1 = at lower)
        total_span = upper_day - lower_day
        position = (upper_day - days_before) / total_span

        return upper_val + (lower_val - upper_val) * position

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            'points': self.points,
            'total_tickets': self.total_tickets,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'SalesCurve':
        """Create SalesCurve from dictionary."""
        return cls(
            points=data.get('points', {}),
            total_tickets=data.get('total_tickets', 0),
        )


@dataclass
class BaselineCurveData:
    """
    Aggregated baseline curve from multiple events.

    Used for projecting expected sales for new events.
    """
    points: dict = field(default_factory=dict)  # days_before -> avg cumulative %
    events_count: int = 0
    segment_type: str = 'global'  # 'global', 'city', 'venue'
    segment_id: Optional[str] = None  # e.g., city name or venue id
    max_historical_capacity: Optional[int] = None  # max capacity of contributing events (when use_capacity)
    points_tickets: Optional[dict] = None  # days_before -> avg cumulative ticket count across events

    def lookup(self, days_before: int) -> float:
        """Look up expected percentage at a given day."""
        curve = SalesCurve(points=self.points, total_tickets=0)
        return curve.lookup(days_before)

    def interpolate(self, days_before: int) -> float:
        """Interpolate value between known points."""
        curve = SalesCurve(points=self.points, total_tickets=0)
        return curve.interpolate(days_before)

    def interpolate_tickets(self, days_before: int) -> float:
        """Interpolate cumulative ticket count at a given day (linear between known days)."""
        if not self.points_tickets:
            return 0.0
        curve = SalesCurve(points=self.points_tickets, total_tickets=0)
        return curve.interpolate(days_before)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            'points': self.points,
            'events_count': self.events_count,
            'segment_type': self.segment_type,
            'segment_id': self.segment_id,
            'max_historical_capacity': self.max_historical_capacity,
            'points_tickets': self.points_tickets,
        }


@dataclass
class PaceMetrics:
    """
    Metrics showing how an event is pacing against expectations.
    """
    days_to_event: int
    current_sold: int
    current_percent: float
    expected_percent: float
    pace_delta: float  # positive = ahead, negative = behind
    projected_total: int
    sellout_date: Optional[str] = None  # ISO date string or None
    status: str = 'on_track'  # 'ahead', 'behind', 'on_track'

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            'days_to_event': self.days_to_event,
            'current_sold': self.current_sold,
            'current_percent': self.current_percent,
            'expected_percent': self.expected_percent,
            'pace_delta': self.pace_delta,
            'projected_total': self.projected_total,
            'sellout_date': self.sellout_date,
            'status': self.status,
        }
