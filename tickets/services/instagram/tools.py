"""Customer-safe tools for the Instagram DM support agent.

The agent speaks to the PUBLIC over an untrusted channel, so this is the whole
attack surface. These tools are deliberately minimal and expose ONLY explicitly
public fields.

Do NOT import or reuse anything from ``tickets/services/chat/tools.py`` — those
are organizer/MCP-internal and leak private data (per-event revenue, P&L, customer
PII, LTV, RFM segments, capacity, internal config). ``build_ig_tools`` returns an
exact allowlist, org-bound via closure so the LLM never controls which org is
queried. A test pins the allowlist so an internal tool can't be added by accident,
plus output-scrubbing tests assert no private data leaks.

Hard rule: no tool returns money, order/customer counts, capacity numbers, other
customers' data, PII, LTV/RFM, or internal config; draft/deleted events are never
surfaced.
"""

from django.db.models import Q
from django.utils import timezone


# ---------------------------------------------------------------------------
# Querysets (customer-visible scoping)
# ---------------------------------------------------------------------------
def _customer_visible_events(organization, direct_statuses=None):
    """Events a customer could reasonably ask about.

    Never deleted, never cancelled. For on-platform (direct) events we gate on status so
    a draft (still being built) never leaks: upcoming listings require LIVE, while past
    listings also admit ENDED — a direct event that actually ran ends up ENDED, not LIVE,
    so requiring LIVE would wrongly hide every past direct show. Pass ``direct_statuses``
    to widen the allowed set (default ``(LIVE,)``). External (CSV-imported) events carry
    no meaningful publication state (``status`` defaults to ``draft`` on import), so they
    are included regardless of status; excluding them would hide the bulk of an
    external-first org's events.
    """
    from tickets.models import (
        EVENT_STATUS_CANCELLED, EVENT_STATUS_LIVE,
        TICKETING_TYPE_DIRECT, TICKETING_TYPE_EXTERNAL, Event,
    )

    if direct_statuses is None:
        direct_statuses = (EVENT_STATUS_LIVE,)

    return (
        Event.objects
        .filter(organization=organization, deleted_at__isnull=True)
        .exclude(status=EVENT_STATUS_CANCELLED)
        .filter(
            Q(ticketing_type=TICKETING_TYPE_EXTERNAL)
            | Q(ticketing_type=TICKETING_TYPE_DIRECT, status__in=direct_statuses)
        )
        .select_related('venue')
    )


def _future_events(qs):
    """Restrict an event queryset to ones that haven't finished yet."""
    today = timezone.localdate()
    return qs.filter(
        Q(end_date__isnull=False, end_date__gte=today)
        | Q(end_date__isnull=True, start_date__gte=today)
    )


def _past_events(qs):
    """Restrict an event queryset to ones that have already finished (complement of
    ``_future_events``)."""
    today = timezone.localdate()
    return qs.filter(
        Q(end_date__isnull=False, end_date__lt=today)
        | Q(end_date__isnull=True, start_date__lt=today)
    )


# ---------------------------------------------------------------------------
# Formatting (public fields only — no money, counts, capacity, or PII)
# ---------------------------------------------------------------------------
def _format_when(event):
    when = event.start_date.strftime('%A, %B %-d, %Y')
    if event.start_time:
        tz_label = event.get_timezone_display() if hasattr(event, 'get_timezone_display') else ''
        time_str = event.start_time.strftime('%-I:%M %p')
        when += f" at {time_str}{(' ' + tz_label) if tz_label else ''}"
    return when


def _format_where(event):
    venue = event.venue
    parts = [p for p in [venue.name, venue.city] if p]
    return ', '.join(parts) if parts else 'Venue TBA'


def _event_line(event):
    line = f"- {event.name} — {_format_when(event)} — {_format_where(event)}"
    if event.ticket_link:
        line += f" — Tickets: {event.ticket_link}"
    return line


# ---------------------------------------------------------------------------
# Tool implementations (org passed explicitly; bound via closure in build_ig_tools)
# ---------------------------------------------------------------------------
def _get_faq(organization, topic: str = "") -> str:
    """Return the org's published FAQs, optionally filtered by topic/question text."""
    from tickets.models import OrgFAQ

    qs = OrgFAQ.objects.filter(
        organization=organization,
        is_published=True,
        deleted_at__isnull=True,
    ).order_by('sort_order', 'created_at')
    if topic:
        qs = qs.filter(Q(topic__icontains=topic) | Q(question__icontains=topic))

    faqs = list(qs[:50])
    if not faqs:
        return "No published FAQs are available for this topic."

    return "\n\n".join(f"Q: {f.question}\nA: {f.answer}" for f in faqs)


def _list_upcoming_events(organization, limit: int = 5) -> str:
    """List upcoming events with public details only. No counts/revenue/capacity."""
    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = 5

    events = list(
        _future_events(_customer_visible_events(organization))
        .order_by('start_date', 'start_time', 'name')[:limit]
    )
    if not events:
        return "There are no upcoming events on the calendar right now."

    return "Upcoming events:\n" + "\n".join(_event_line(e) for e in events)


def _list_past_events(organization, limit: int = 5) -> str:
    """List recent past events, most recent first. Public details only — no counts/
    revenue/capacity. Answers 'when was your last event?' and similar history questions."""
    from tickets.models import EVENT_STATUS_ENDED, EVENT_STATUS_LIVE

    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = 5

    # A direct event that already ran is ENDED (not LIVE), so include both here — while
    # still excluding DRAFT (never published) and CANCELLED.
    visible = _customer_visible_events(
        organization, direct_statuses=(EVENT_STATUS_LIVE, EVENT_STATUS_ENDED),
    )
    events = list(
        _past_events(visible).order_by('-start_date', '-start_time', 'name')[:limit]
    )
    if not events:
        return "There are no past events on record."

    return "Past events (most recent first):\n" + "\n".join(_event_line(e) for e in events)


def _find_event(organization, query: str) -> str:
    """Find an event by name, city, or date. Returns public details + an availability
    hint (no numbers)."""
    query = (query or "").strip()
    if not query:
        return "Please tell me the event name you're asking about."

    matches = list(
        _future_events(_customer_visible_events(organization))
        .filter(Q(name__icontains=query) | Q(venue__city__icontains=query))
        .order_by('start_date', 'start_time', 'name')[:5]
    )
    if not matches:
        return f"I couldn't find an event matching '{query}'."

    blocks = []
    for e in matches:
        lines = [
            e.name,
            f"When: {_format_when(e)}",
            f"Where: {_format_where(e)}",
        ]
        if e.summary:
            lines.append(f"About: {e.summary}")
        elif e.description:
            lines.append(f"About: {e.description[:300]}")
        if e.ticket_link:
            lines.append(f"Tickets are available here: {e.ticket_link}")
        else:
            lines.append("For availability and tickets, check our page or ask us here.")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def _get_contact_info(organization) -> str:
    """Return the org's public contact details for routing/escalation phrasing."""
    lines = [f"Organizer: {organization.name}"]
    website = getattr(organization, 'website', '')
    instagram_url = getattr(organization, 'instagram_url', '')
    if website:
        lines.append(f"Website: {website}")
    if instagram_url:
        lines.append(f"Instagram: {instagram_url}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Allowlisted tool builder
# ---------------------------------------------------------------------------
def build_ig_tools(organization):
    """Return the exact, org-bound allowlist of customer-safe LangChain tools.

    Each tool closes over ``organization`` so the LLM never chooses which org is
    queried. The returned ``.name`` set is pinned by a test.
    """
    from langchain_core.tools import tool

    org = organization

    @tool
    def get_faq(topic: str = "") -> str:
        """Look up the organizer's published FAQ answers. Optionally pass a topic or
        keyword to narrow the results."""
        return _get_faq(org, topic=topic)

    @tool
    def list_upcoming_events(limit: int = 5) -> str:
        """List the organizer's upcoming events with date, time, venue, and ticket link."""
        return _list_upcoming_events(org, limit=limit)

    @tool
    def list_past_events(limit: int = 5) -> str:
        """List the organizer's recent past events, most recent first, with date and
        venue. Use for questions about event history, e.g. 'when was your last event?'
        or 'what shows have you done before?'."""
        return _list_past_events(org, limit=limit)

    @tool
    def find_event(query: str) -> str:
        """Find a specific event by name, city, or date and return its public details
        and how to get tickets."""
        return _find_event(org, query=query)

    @tool
    def get_contact_info() -> str:
        """Get the organizer's public contact details (name, website, Instagram)."""
        return _get_contact_info(org)

    return [get_faq, list_upcoming_events, list_past_events, find_event, get_contact_info]
