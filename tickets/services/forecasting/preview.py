"""
Forecast preview service.

Generates ticket sales projections for hypothetical events without
requiring an Event record to be created in the database.
"""
from datetime import date
from typing import Optional
from uuid import UUID

from django.db.models import Count, Max
from django.utils import timezone

from tickets.models import Event, Venue
from .historical_aggregator import HistoricalAggregator
from .sales_curve import SalesCurveCalculator


def generate_forecast_preview(
    venue_id: Optional[str],
    event_date: date,
    capacity: int,
    min_events: int = 1,
    starting_tickets: Optional[int] = None,
    organization=None,
) -> dict:
    """
    Generate a forecast preview for a hypothetical event.

    This function creates a sales projection based on historical data
    without needing to create an actual Event record.

    Curve points are limited to the range 0..days_until_event (and -1 for
    day-of), so the chart shows only the event's sales window. Past events
    return no curve points.

    When starting_tickets is set, the curve is for an event already in progress:
    "today" shows that ticket count and the curve from today to event day keeps
    the same shape (pace) as the baseline but is shifted and scaled accordingly.

    Args:
        venue_id: UUID string of the venue (optional, uses global if None)
        event_date: The date of the hypothetical event
        capacity: Expected total ticket capacity
        min_events: Minimum events required for valid baseline (at least one)
        starting_tickets: Optional tickets already sold; when set, curve starts at this value
        organization: Optional organization to scope baseline and venue capacity to

    Returns:
        dict with:
            - curve_points: list of {days_before, expected_tickets, expected_percent}
            - baseline_info: {segment_type, segment_name, events_count}
            - has_sufficient_data: bool
    """
    aggregator = HistoricalAggregator(min_events=min_events, organization=organization)

    # Get venue if provided
    venue = None
    if venue_id:
        try:
            venue = Venue.objects.get(id=UUID(venue_id))
        except (Venue.DoesNotExist, ValueError):
            pass

    # Days from today until the event (used to limit x-axis and to filter baseline)
    today = timezone.now().date()
    days_until_event = (event_date - today).days

    # Get baseline curve (filtered by sales length when event is in the future)
    reference = days_until_event if days_until_event > 0 else None
    baseline = aggregator.get_baseline_curve(
        venue=venue, reference_days_before=reference
    )

    # Build baseline info
    if baseline.segment_type == 'venue' and venue:
        segment_name = str(venue)
    elif baseline.segment_type == 'city' and baseline.segment_id:
        segment_name = baseline.segment_id
    else:
        segment_name = 'All Events'

    # When a venue is selected, use that venue's max historical capacity for the warning
    # (so the warning reflects the selected venue even if the baseline fell back to city/global).
    # Only consider past events with at least one ticket order.
    max_capacity_for_warning = baseline.max_historical_capacity
    if venue is not None:
        venue_events = Event.objects.filter(
            venue=venue,
            start_date__lt=today,
            capacity__isnull=False,
        ).exclude(capacity=0).annotate(
            order_count=Count('ticket_orders')
        ).filter(order_count__gt=0)
        if organization is not None:
            venue_events = venue_events.filter(organization=organization)
        venue_max = venue_events.aggregate(Max('capacity'))['capacity__max']
        if venue_max is not None:
            max_capacity_for_warning = venue_max

    baseline_info = {
        'segment_type': baseline.segment_type,
        'segment_name': segment_name,
        'events_count': baseline.events_count,
        'max_historical_capacity': baseline.max_historical_capacity,
    }

    # Extrapolation warning when entered capacity far exceeds historical scale
    if max_capacity_for_warning and capacity > 2 * max_capacity_for_warning:
        baseline_info['extrapolation_warning'] = True
        baseline_info['extrapolation_message'] = (
            f"Forecast extrapolates beyond historical capacity "
            f"(max historical capacity was {max_capacity_for_warning}). Use with caution."
        )
    else:
        baseline_info['extrapolation_warning'] = False
        baseline_info['extrapolation_message'] = None

    # Check if we have sufficient data
    has_sufficient_data = baseline.events_count >= min_events

    if baseline.events_count == 0:
        return {
            'curve_points': [],
            'baseline_info': baseline_info,
            'has_sufficient_data': False,
            'historical_events': [],
        }

    # Build historical_events list (same segment as baseline)
    city_for_events = venue.city if venue else None
    events_queryset = aggregator.get_events_used_for_baseline(
        city=city_for_events,
        venue=venue,
        reference_days_before=reference,
    )
    curve_calculator = SalesCurveCalculator()
    historical_events = []
    for event in events_queryset:
        curve = curve_calculator.get_event_sales_curve(event)
        if curve.total_tickets > 0:
            event_date_str = event.start_date.isoformat()
            venue_name = f"{event.venue.name}, {event.venue.city}"
            historical_events.append({
                'name': event.name,
                'event_date': event_date_str,
                'venue_name': venue_name,
                'total_tickets_sold': curve.total_tickets,
            })

    # When a venue is selected but baseline fell back to city/global, do not show
    # fallback events in the table (misleading). Return empty list and a message.
    if venue is not None and baseline.segment_type != 'venue':
        historical_events = []
        baseline_info['venue_no_data_fallback'] = True
        baseline_info['fallback_message'] = (
            f"No past events with order data at this venue. Baseline uses {segment_name}."
        )
    else:
        baseline_info['venue_no_data_fallback'] = False
        baseline_info['fallback_message'] = None

    # Past event: no curve points
    if days_until_event < 0:
        return {
            'curve_points': [],
            'baseline_info': baseline_info,
            'has_sufficient_data': has_sufficient_data,
            'historical_events': historical_events,
        }

    curve_points = []

    if baseline.points_tickets:
        # Forecast by historical absolute ticket counts: re-anchor at "today", cap at capacity
        tickets_at_start = baseline.interpolate_tickets(days_until_event)
        tickets_at_end = baseline.interpolate_tickets(-1)
        max_incremental = max(0.0, tickets_at_end - tickets_at_start)
        end_cap = min(capacity, max_incremental) if max_incremental > 0 else 0

        if max_incremental > 0 and end_cap > 0:
            for day in range(days_until_event, -2, -1):
                incremental = baseline.interpolate_tickets(day) - tickets_at_start
                relative = (incremental / max_incremental) if max_incremental > 0 else 0.0
                relative = max(0.0, min(1.0, relative))
                expected_tickets = min(capacity, round(relative * end_cap))
                expected_percent = round(100 * expected_tickets / capacity, 2) if capacity > 0 else 0.0
                curve_points.append({
                    'days_before': day,
                    'expected_tickets': expected_tickets,
                    'expected_percent': expected_percent,
                })
        # When historical sales in the forecast window are zero (baseline flat in [days_until_event, -1]),
        # map the forecast window onto the baseline's full range so the chart shows the historical shape.
        else:
            pts = baseline.points_tickets or baseline.points
            baseline_start_day = max(pts.keys()) if pts else days_until_event
            baseline_end_day = -1
            forecast_window_len = float(days_until_event - baseline_end_day)  # e.g. 31
            tickets_at_end = baseline.interpolate_tickets(-1) if baseline.points_tickets else 0.0
            for day in range(days_until_event, -2, -1):
                t = (day - days_until_event) / (-1 - days_until_event) if forecast_window_len else 0.0
                t = max(0.0, min(1.0, t))
                baseline_day = baseline_start_day + t * (baseline_end_day - baseline_start_day)
                baseline_day = int(round(baseline_day))
                if baseline.points_tickets and tickets_at_end > 0:
                    tickets_at_b = baseline.interpolate_tickets(baseline_day)
                    sell_through_pct = 100.0 * tickets_at_b / tickets_at_end
                    # Cap expected tickets at historical sales so forecast level matches baseline
                    expected_tickets = min(capacity, int(round(tickets_at_end * sell_through_pct / 100.0)))
                else:
                    sell_through_pct = baseline.interpolate(baseline_day)
                    sell_through_pct = max(0.0, min(100.0, sell_through_pct))
                    expected_tickets = int(round(capacity * sell_through_pct / 100.0))
                expected_percent = round(100.0 * expected_tickets / capacity, 2) if capacity > 0 else 0.0
                curve_points.append({
                    'days_before': day,
                    'expected_tickets': expected_tickets,
                    'expected_percent': expected_percent,
                })
    else:
        # Fallback: sell-through percentage (legacy)
        pct_at_start = baseline.interpolate(days_until_event)
        pct_at_end = baseline.interpolate(-1)
        span = pct_at_end - pct_at_start
        if span <= 0:
            span = 1.0

        for day in range(days_until_event, -2, -1):
            raw_pct = baseline.interpolate(day)
            relative_pct = (raw_pct - pct_at_start) / span
            relative_pct = max(0.0, min(1.0, relative_pct))
            expected_tickets = int(capacity * relative_pct * (pct_at_end / 100))
            expected_percent = round(relative_pct * pct_at_end, 2)
            curve_points.append({
                'days_before': day,
                'expected_tickets': expected_tickets,
                'expected_percent': expected_percent,
            })

    # Re-anchor curve when event is already in progress (starting_tickets set)
    if starting_tickets is not None and starting_tickets >= 0 and curve_points:
        if starting_tickets >= capacity:
            # Sold out: flat line at capacity
            for pt in curve_points:
                pt['expected_tickets'] = capacity
                pt['expected_percent'] = round(100.0 * capacity / capacity, 2) if capacity > 0 else 0.0
        else:
            tickets_at_today = curve_points[0]['expected_tickets']
            tickets_at_end = curve_points[-1]['expected_tickets']
            incremental_baseline = tickets_at_end - tickets_at_today
            if incremental_baseline <= 0:
                projected_end = min(capacity, starting_tickets)
            else:
                projected_end = min(capacity, starting_tickets + incremental_baseline)
            for pt in curve_points:
                if incremental_baseline <= 0:
                    new_tickets = starting_tickets
                else:
                    new_tickets = starting_tickets + (
                        (pt['expected_tickets'] - tickets_at_today)
                        / incremental_baseline
                        * (projected_end - starting_tickets)
                    )
                new_tickets = max(0, min(capacity, round(new_tickets)))
                pt['expected_tickets'] = int(new_tickets)
                pt['expected_percent'] = (
                    round(100.0 * new_tickets / capacity, 2) if capacity > 0 else 0.0
                )

    return {
        'curve_points': curve_points,
        'baseline_info': baseline_info,
        'has_sufficient_data': has_sufficient_data,
        'historical_events': historical_events,
    }
