"""Format event data into plain text for the Upcoming Events knowledge base doc."""
import logging
import platform
from datetime import date

from django.db.models import Prefetch
from django.utils import timezone

from tickets.models import Event, EventCustomFieldValue, Organization

logger = logging.getLogger(__name__)

DAYS_OF_WEEK = [
    'Monday', 'Tuesday', 'Wednesday', 'Thursday',
    'Friday', 'Saturday', 'Sunday',
]


def _format_time(t):
    """Format a time object as '2:00 PM' style."""
    if t is None:
        return 'TBD'
    # Windows uses %#I, Unix uses %-I for non-zero-padded hour
    if platform.system() == 'Windows':
        return t.strftime('%#I:%M %p')
    return t.strftime('%-I:%M %p')


def _format_date(d):
    """Format a date as 'March 15, 2026'."""
    if platform.system() == 'Windows':
        return d.strftime('%B %#d, %Y')
    return d.strftime('%B %-d, %Y')


class EventDocFormatter:
    """Builds plain text content for the Upcoming Events knowledge base doc."""

    def __init__(self, organization):
        self.organization = organization

    def get_upcoming_events(self):
        """Query upcoming events for the organization, sorted by date."""
        today = timezone.localdate()
        return list(
            Event.objects.filter(
                organization=self.organization,
                start_date__gte=today,
                deleted_at__isnull=True,
            )
            .select_related('venue')
            .prefetch_related(
                'talent_lineup',
                Prefetch(
                    'custom_field_values',
                    queryset=EventCustomFieldValue.objects.select_related(
                        'custom_field', 'custom_field_option'
                    ),
                ),
            )
            .order_by('start_date', 'start_time', 'name')
        )

    def _format_event(self, event):
        """Format a single event into a text block."""
        venue = event.venue
        day_of_week = DAYS_OF_WEEK[event.start_date.weekday()]
        lines = []

        # Header
        lines.append('=' * 60)
        lines.append(f'{event.name} — {_format_date(event.start_date)}')
        lines.append('=' * 60)
        lines.append('')

        # Date and time
        lines.append(f'Date: {_format_date(event.start_date)} ({day_of_week})')
        lines.append(f'Doors Open: {_format_time(event.start_time)}')
        if event.end_time:
            lines.append(f'Event Ends: {_format_time(event.end_time)}')
        elif event.end_date:
            lines.append(f'Event Ends: {_format_date(event.end_date)}')
        lines.append('')

        # Venue
        lines.append(f'Venue: {venue.name}')
        address = venue.get_display_address()
        if address:
            lines.append(f'Address: {address}')
        if venue.city:
            lines.append(f'City: {venue.city}')
        lines.append('')

        # Capacity
        capacity = event.capacity or venue.capacity
        if capacity:
            lines.append(f'Venue Capacity: {capacity}')
            lines.append('')

        # Tickets
        if event.ticket_link:
            lines.append(f'Tickets: {event.ticket_link}')
        else:
            lines.append('Tickets: DM us for ticket info or check our link in bio!')
        lines.append('')

        # Custom field values
        custom_values = {}
        for cfv in event.custom_field_values.all():
            if cfv.custom_field_option:
                custom_values[cfv.custom_field.name] = cfv.custom_field_option.label

        for field_name, value in custom_values.items():
            lines.append(f'{field_name}: {value}')
        if custom_values:
            lines.append('')

        # Talent lineup
        talent = list(event.talent_lineup.all().order_by('order'))
        if talent:
            talent_names = ', '.join(t.name for t in talent)
            lines.append(f'Talent / DJ Lineup: {talent_names}')
            lines.append('')

        # Description
        if event.description:
            lines.append(f'Description: {event.description}')
            lines.append('')

        # Common Questions Q&A
        lines.append('--- Common Questions ---')
        lines.append('')

        lines.append(f'Q: When is {event.name}?')
        lines.append(
            f'A: {day_of_week}, {_format_date(event.start_date)}. '
            f'Doors open at {_format_time(event.start_time)}.'
        )
        lines.append('')

        lines.append(f'Q: Where is {event.name}?')
        addr_part = f' at {address}' if address else ''
        lines.append(f'A: {venue.name}{addr_part} in {venue.city}.')
        lines.append('')

        lines.append(f'Q: How do I get tickets for {event.name}?')
        if event.ticket_link:
            lines.append(f'A: You can get tickets here: {event.ticket_link}')
        else:
            lines.append('A: DM us for ticket info or check our link in bio!')
        lines.append('')

        if 'Age requirement' in custom_values:
            lines.append(f'Q: Is there an age requirement for {event.name}?')
            lines.append(f'A: Yes, this event is {custom_values["Age requirement"]}.')
            lines.append('')

        if 'Dress Code' in custom_values:
            lines.append(f'Q: What is the dress code for {event.name}?')
            lines.append(f'A: {custom_values["Dress Code"]}')
            lines.append('')

        if 'Re-entry allowed' in custom_values:
            lines.append(f'Q: Is re-entry allowed at {event.name}?')
            val = custom_values['Re-entry allowed']
            if val.lower() == 'yes':
                lines.append('A: Yes, re-entry is allowed.')
            else:
                lines.append('A: No, re-entry is not allowed.')
            lines.append('')

        if 'Food Available' in custom_values:
            lines.append(f'Q: Is food available at {event.name}?')
            val = custom_values['Food Available']
            if val.lower() == 'yes':
                lines.append('A: Yes, food is available.')
            else:
                lines.append('A: No, food is not available at this event.')
            lines.append('')

        if 'Parking Info' in custom_values:
            lines.append(f'Q: What about parking for {event.name}?')
            lines.append(f'A: {custom_values["Parking Info"]}')
            lines.append('')

        lines.append('')
        return '\n'.join(lines)

    def generate_full_document(self):
        """Generate the complete document content as plain text."""
        events = self.get_upcoming_events()
        org_name = self.organization.name.upper()

        sections = []

        # Header
        sections.append(f'{org_name} — UPCOMING EVENTS')
        sections.append('Knowledge Base for AI Instagram DM Agent')
        sections.append('')
        sections.append(f'Last updated: {_format_date(timezone.localdate())}')
        sections.append(f'Total upcoming events: {len(events)}')
        sections.append('')

        if not events:
            sections.append(
                'No upcoming events at this time. '
                'Follow us on Instagram or check our website for announcements!'
            )
            sections.append('')
        else:
            for event in events:
                sections.append(self._format_event(event))

        # Footer
        sections.append('---')
        sections.append(
            f'Auto-generated from the {self.organization.name} event database.'
        )

        return '\n'.join(sections)
