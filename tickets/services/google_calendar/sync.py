"""Send event payloads to Pipedream webhook for Google Calendar sync."""
import logging
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

# Custom field names used for Google Calendar description (org-defined dropdowns).
EVENT_TYPE_FIELD_NAME = 'Event Type'
INDOORS_OUTDOORS_FIELD_NAME = 'Indoors/Outdoors'


def _get_custom_field_display_by_name(event):
    """
    Return a dict of custom field name -> selected option label for this event.
    Uses one query with select_related for custom_field and custom_field_option.
    """
    from tickets.models import EventCustomFieldValue
    qs = EventCustomFieldValue.objects.filter(
        event=event,
    ).select_related('custom_field', 'custom_field_option')
    return {
        cfv.custom_field.name: (cfv.custom_field_option.label if cfv.custom_field_option else '')
        for cfv in qs
    }


def _build_google_calendar_description(event, custom_values):
    """
    Build description in the format:
    Event Type: [event_type]
    Venue Capacity: [capacity]
    Indoors/Outdoors: [custom_field_response]
    """
    event_type = custom_values.get(EVENT_TYPE_FIELD_NAME, '')
    capacity = event.capacity or (event.venue.capacity if event.venue else None)
    capacity_str = str(capacity) if capacity is not None else ''
    indoors_outdoors = custom_values.get(INDOORS_OUTDOORS_FIELD_NAME, '')

    lines = [
        f"Event Type: {event_type}",
        f"Venue Capacity: {capacity_str}",
        f"Indoors/Outdoors: {indoors_outdoors}",
    ]
    return '\n'.join(lines)


def _format_location(venue):
    """
    Format venue as Google Calendar location string:
    "Vue Lounge, 2326 2nd Avenue Seattle , WA 98121 , United States"
    """
    parts = [venue.name]
    address_parts = []
    if venue.street_address:
        address_parts.append(venue.street_address)
    if venue.city:
        address_parts.append(venue.city)
    if address_parts:
        line1 = ' '.join(address_parts)
        parts.append(line1)
    if venue.state or venue.postal_code:
        line2 = ' '.join(p for p in (venue.state, venue.postal_code) if p)
        parts.append(line2)
    if venue.country:
        parts.append(venue.country)
    # Join with " , " (space-comma-space) between address components after the venue name
    if len(parts) == 1:
        return parts[0]
    return parts[0] + ', ' + ' , '.join(parts[1:])


def _format_datetime_iso(date, time):
    """Format date and time as YYYY-MM-DDTHH:MM:SS (local time, no zone in string)."""
    if time is None:
        return None
    dt = datetime.combine(date, time)
    return dt.strftime('%Y-%m-%dT%H:%M:%S')


def build_pipedream_payload(event):
    """
    Build JSON payload for Pipedream webhook.

    When the event has start_time, start and end are ISO datetimes (YYYY-MM-DDTHH:MM:SS).
    When all-day (no start_time), start and end are date-only (YYYY-MM-DD).
    timezone is always included for the event's timezone.
    """
    venue = event.venue
    location = _format_location(venue)
    custom_values = _get_custom_field_display_by_name(event)
    description = _build_google_calendar_description(event, custom_values)
    tz = event.timezone or 'America/Los_Angeles'

    if event.start_time is not None:
        start = _format_datetime_iso(event.start_date, event.start_time)
        if event.end_date and event.end_time:
            end = _format_datetime_iso(event.end_date, event.end_time)
        elif event.end_date and not event.end_time:
            end = _format_datetime_iso(event.end_date, event.start_time)
        else:
            end_dt = datetime.combine(event.start_date, event.start_time) + timedelta(hours=3)
            end = end_dt.strftime('%Y-%m-%dT%H:%M:%S')
    else:
        start = event.start_date.strftime('%Y-%m-%d')
        end_date = event.end_date or (event.start_date + timedelta(days=1))
        end = end_date.strftime('%Y-%m-%d')

    return {
        'title': event.name,
        'summary': event.name,
        'location': location,
        'description': description,
        'start': start,
        'end': end,
        'timezone': tz,
    }


def send_event_to_pipedream(webhook_url, event, timeout=10):
    """
    POST event payload to Pipedream webhook. Fails silently on error.

    Returns (success: bool, google_event_id: str | None). If the workflow
    returns a body with an event id (e.g. from the Google Calendar step), we pass it through.
    """
    payload = build_pipedream_payload(event)
    try:
        resp = requests.post(webhook_url, json=payload, timeout=timeout)
        resp.raise_for_status()
        google_event_id = None
        if resp.text:
            try:
                data = resp.json()
                google_event_id = (
                    data.get('id')
                    or (data.get('data', {}) or {}).get('id')
                )
            except Exception:
                pass
        return True, google_event_id
    except requests.RequestException as e:
        logger.exception("Pipedream webhook error: %s", e)
        return False, None
