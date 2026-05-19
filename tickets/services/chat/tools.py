"""
Org-scoped tool functions for the Cue chat agent.

Each function accepts `organization` as its first argument. At agent construction
time, `functools.partial` binds the real org so the LLM never controls which org
is queried.
"""

import os
from datetime import date
from decimal import Decimal

from django.db.models import Sum, Count, Avg, Q, F
from django.db.models.functions import Coalesce
from django.utils import timezone


# ---------------------------------------------------------------------------
# 1. Organization summary
# ---------------------------------------------------------------------------
def _get_organization_summary(organization) -> str:
    """Get a high-level summary of the organization's data."""
    from tickets.models import Customer, Event, TicketOrder

    customer_count = Customer.objects.filter(organization=organization).exclude(email__endswith='@placeholder.local').count()
    event_count = Event.objects.filter(organization=organization, deleted_at__isnull=True).count()
    order_stats = TicketOrder.objects.filter(
        customer__organization=organization,
    ).aggregate(
        order_count=Count('id'),
        total_revenue=Coalesce(Sum('total_amount'), Decimal('0.00')),
    )
    avg_ltv = Customer.objects.filter(
        organization=organization,
    ).aggregate(avg=Coalesce(Avg('lifetime_value'), Decimal('0.00')))['avg']

    return (
        f"Organization: {organization.name}\n"
        f"Total customers: {customer_count}\n"
        f"Total events: {event_count}\n"
        f"Total orders: {order_stats['order_count']}\n"
        f"Total revenue: ${order_stats['total_revenue']:,.2f}\n"
        f"Average customer LTV: ${avg_ltv:,.2f}"
    )


# ---------------------------------------------------------------------------
# 2. Search customers
# ---------------------------------------------------------------------------
def _search_customers(organization, query: str = "", segment: str = "", limit: int = 10) -> str:
    """Search customers by name, email, or RFM segment. Returns top N matches."""
    from tickets.models import Customer

    qs = Customer.objects.filter(organization=organization)
    if query:
        qs = qs.filter(Q(name__icontains=query) | Q(email__icontains=query))
    if segment:
        qs = qs.filter(rfm_segment__iexact=segment)
    qs = qs.order_by('-lifetime_value')[:limit]

    if not qs.exists():
        return "No customers found matching your criteria."

    lines = []
    for c in qs:
        seg = f" [{c.rfm_segment}]" if c.rfm_segment else ""
        lines.append(f"- {c.name} ({c.email}) — LTV: ${c.lifetime_value:,.2f}{seg}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. Customer detail
# ---------------------------------------------------------------------------
def _get_customer_detail(organization, email: str) -> str:
    """Get full profile and last 10 orders for a customer by email."""
    from tickets.models import Customer, TicketOrder

    try:
        customer = Customer.objects.get(organization=organization, email__iexact=email)
    except Customer.DoesNotExist:
        return f"No customer found with email: {email}"

    orders = TicketOrder.objects.filter(
        customer=customer,
    ).select_related('event').order_by('-order_date')[:10]

    lines = [
        f"Name: {customer.name}",
        f"Email: {customer.email}",
        f"Phone: {customer.phone or 'N/A'}",
        f"Lifetime Value: ${customer.lifetime_value:,.2f}",
        f"Last Order: {customer.last_order_date or 'N/A'}",
        f"RFM Segment: {customer.rfm_segment or 'Not scored'}",
        f"RFM Scores: R={customer.rfm_recency_score} F={customer.rfm_frequency_score} M={customer.rfm_monetary_score}",
        "",
        "Recent Orders:",
    ]
    for o in orders:
        lines.append(f"  - {o.order_date.strftime('%Y-%m-%d')} | {o.event.name} | ${o.total_amount:,.2f} | #{o.order_number}")

    if not orders:
        lines.append("  No orders found.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. Search events
# ---------------------------------------------------------------------------
def _search_events(organization, query: str = "", year: int = 0, limit: int = 10) -> str:
    """Search events by name or year with revenue annotations."""
    from tickets.models import Event

    qs = Event.objects.filter(organization=organization, deleted_at__isnull=True)
    if query:
        qs = qs.filter(Q(name__icontains=query) | Q(venue__name__icontains=query) | Q(venue__city__icontains=query))
    if year:
        qs = qs.filter(start_date__year=year)

    qs = qs.select_related('venue').annotate(
        total_revenue=Coalesce(Sum('ticket_orders__total_amount'), Decimal('0.00')),
        order_count=Count('ticket_orders'),
    ).order_by('-start_date')[:limit]

    if not qs.exists():
        return "No events found matching your criteria."

    lines = []
    for e in qs:
        lines.append(
            f"- {e.name} | {e.venue.name}, {e.venue.city} | {e.start_date} | "
            f"Revenue: ${e.total_revenue:,.2f} | Orders: {e.order_count}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. Event detail
# ---------------------------------------------------------------------------
def _get_event_detail(organization, event_name: str) -> str:
    """Get full financial details for an event by name (closest match)."""
    from tickets.models import Event, EventExpense, EventIncome

    event = Event.objects.filter(
        organization=organization,
        deleted_at__isnull=True,
        name__icontains=event_name,
    ).select_related('venue').first()

    if not event:
        return f"No event found matching: {event_name}"

    revenue = event.ticket_orders.aggregate(
        total=Coalesce(Sum('total_amount'), Decimal('0.00')),
        count=Count('id'),
    )
    expenses = EventExpense.objects.visible().filter(event=event).aggregate(
        total=Coalesce(Sum('amount'), Decimal('0.00')),
    )
    additional_income = EventIncome.objects.filter(event=event).aggregate(
        total=Coalesce(Sum('amount'), Decimal('0.00')),
    )

    total_income = revenue['total'] + additional_income['total']
    profit = total_income - expenses['total']

    lines = [
        f"Event: {event.name}",
        f"Venue: {event.venue.name}, {event.venue.city}",
        f"Date: {event.start_date}",
        f"Capacity: {event.capacity or 'N/A'}",
        "",
        "Financials:",
        f"  Ticket Revenue: ${revenue['total']:,.2f} ({revenue['count']} orders)",
        f"  Additional Income: ${additional_income['total']:,.2f}",
        f"  Total Income: ${total_income:,.2f}",
        f"  Total Expenses: ${expenses['total']:,.2f}",
        f"  Net Profit: ${profit:,.2f}",
    ]

    # Expense breakdown
    expense_items = EventExpense.objects.visible().filter(event=event).values('category').annotate(
        total=Sum('amount'),
    ).order_by('-total')
    if expense_items:
        lines.append("")
        lines.append("Expense Breakdown:")
        for item in expense_items:
            lines.append(f"  - {item['category'].title()}: ${item['total']:,.2f}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 6. Segment distribution
# ---------------------------------------------------------------------------
def _get_segment_distribution(organization) -> str:
    """Get RFM segment counts and percentages."""
    from tickets.models import Customer

    total = Customer.objects.filter(organization=organization).count()
    if total == 0:
        return "No customers in this organization."

    segments = Customer.objects.filter(
        organization=organization,
    ).exclude(
        rfm_segment='',
    ).values('rfm_segment').annotate(
        count=Count('id'),
    ).order_by('-count')

    unscored = Customer.objects.filter(organization=organization, rfm_segment='').count()

    lines = ["RFM Segment Distribution:"]
    for seg in segments:
        pct = seg['count'] / total * 100
        lines.append(f"  - {seg['rfm_segment']}: {seg['count']} ({pct:.1f}%)")
    if unscored:
        pct = unscored / total * 100
        lines.append(f"  - Not Scored: {unscored} ({pct:.1f}%)")
    lines.append(f"\nTotal customers: {total}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7. Top customers
# ---------------------------------------------------------------------------
def _get_top_customers(organization, metric: str = "ltv", limit: int = 10) -> str:
    """Get top N customers by LTV or order count."""
    from tickets.models import Customer

    qs = Customer.objects.filter(organization=organization)

    if metric == "orders":
        qs = qs.annotate(order_count=Count('ticket_orders')).order_by('-order_count')[:limit]
        lines = ["Top Customers by Order Count:"]
        for c in qs:
            seg = f" [{c.rfm_segment}]" if c.rfm_segment else ""
            lines.append(f"  - {c.name} ({c.email}) — {c.order_count} orders, LTV: ${c.lifetime_value:,.2f}{seg}")
    else:
        qs = qs.order_by('-lifetime_value')[:limit]
        lines = ["Top Customers by Lifetime Value:"]
        for c in qs:
            seg = f" [{c.rfm_segment}]" if c.rfm_segment else ""
            lines.append(f"  - {c.name} ({c.email}) — LTV: ${c.lifetime_value:,.2f}{seg}")

    if not lines[1:]:
        return "No customers found."
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 8. Revenue by venue
# ---------------------------------------------------------------------------
def _get_revenue_by_venue(organization) -> str:
    """Get total revenue grouped by venue/city."""
    from tickets.models import TicketOrder

    venues = TicketOrder.objects.filter(
        customer__organization=organization,
    ).values(
        venue_name=F('event__venue__name'),
        venue_city=F('event__venue__city'),
    ).annotate(
        total_revenue=Sum('total_amount'),
        order_count=Count('id'),
    ).order_by('-total_revenue')

    if not venues:
        return "No revenue data available."

    lines = ["Revenue by Venue:"]
    for v in venues:
        lines.append(
            f"  - {v['venue_name']}, {v['venue_city']} — "
            f"${v['total_revenue']:,.2f} ({v['order_count']} orders)"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 9. Repeat customer stats
# ---------------------------------------------------------------------------
def _get_repeat_customer_stats(organization) -> str:
    """Get new vs. returning customer breakdown per event."""
    from tickets.services.cohort_analysis.repeat_customer_calculator import RepeatCustomerCalculator

    calc = RepeatCustomerCalculator(organization)
    result = calc.calculate()
    summary = result['summary']

    lines = [
        "Repeat Customer Summary:",
        f"  Total attendees: {summary['total_attendees']}",
        f"  New: {summary['new_count']} ({summary['new_pct']}%)",
        f"  Returning: {summary['returning_count']} ({summary['returning_pct']}%)",
        "",
        "Per Event:",
    ]
    for e in result['events'][-10:]:
        lines.append(
            f"  - {e['event_name']} ({e['event_date']}) — "
            f"{e['total']} attendees, {e['returning_count']} returning ({e['returning_pct']}%)"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 10. Cohort retention
# ---------------------------------------------------------------------------
def _get_cohort_retention(organization) -> str:
    """Get monthly cohort retention data."""
    from tickets.services.cohort_analysis.cohort_retention_calculator import CohortRetentionCalculator

    calc = CohortRetentionCalculator(organization)
    result = calc.calculate(max_periods=6)
    summary = result['summary']

    lines = [
        "Cohort Retention Summary:",
        f"  Total cohorts: {summary['total_cohorts']}",
        f"  Avg M1 retention: {summary['avg_m1_retention']}%",
        f"  Avg M3 retention: {summary['avg_m3_retention']}%",
        "",
        "Cohorts (last 6):",
    ]
    for cohort in result['cohorts'][-6:]:
        retention_str = ", ".join(
            f"M{p['period']}={p['retention_pct']}%" for p in cohort['periods'][:7]
        )
        lines.append(f"  - {cohort['cohort']} (n={cohort['size']}): {retention_str}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 11. Upcoming events
# ---------------------------------------------------------------------------
def _get_upcoming_events(organization, limit: int = 10) -> str:
    """Get future events sorted by date."""
    from tickets.models import Event

    today = date.today()
    events = Event.objects.filter(
        organization=organization,
        deleted_at__isnull=True,
        start_date__gte=today,
    ).select_related('venue').order_by('start_date')[:limit]

    if not events:
        return "No upcoming events found."

    lines = ["Upcoming Events:"]
    for e in events:
        time_str = f" at {e.start_time.strftime('%I:%M %p')}" if e.start_time else ""
        cap = f" (capacity: {e.capacity})" if e.capacity else ""
        lines.append(f"  - {e.start_date}{time_str} | {e.name} | {e.venue.name}, {e.venue.city}{cap}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 12. Knowledge base
# ---------------------------------------------------------------------------
def _get_knowledge_base(organization, topic: str = "") -> str:
    """Read markdown files from the tickets/kb/ directory for general knowledge."""
    kb_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'kb')
    if not os.path.isdir(kb_dir):
        return "No knowledge base articles available."

    articles = []
    for filename in sorted(os.listdir(kb_dir)):
        if not filename.endswith('.md'):
            continue
        if topic and topic.lower() not in filename.lower():
            continue
        filepath = os.path.join(kb_dir, filename)
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read(4000)
        articles.append(f"## {filename}\n{content}")

    if not articles:
        return f"No knowledge base articles found{' for topic: ' + topic if topic else ''}."
    return "\n\n".join(articles)


# ---------------------------------------------------------------------------
# Build tool list with org binding
# ---------------------------------------------------------------------------
def build_tools(organization):
    """Return a list of LangChain tools with the organization pre-bound.

    Each tool is defined inline with a closure over `organization` so that
    LangChain can introspect the function signature for argument schemas.
    """
    from langchain_core.tools import tool

    org = organization

    @tool
    def get_organization_summary() -> str:
        """Get a high-level summary of the organization: total customers, events, orders, revenue, avg LTV."""
        return _get_organization_summary(org)

    @tool
    def search_customers(query: str = "", segment: str = "", limit: int = 10) -> str:
        """Search customers by name, email, or RFM segment."""
        return _search_customers(org, query=query, segment=segment, limit=limit)

    @tool
    def get_customer_detail(email: str) -> str:
        """Get full customer profile and recent orders by email."""
        return _get_customer_detail(org, email=email)

    @tool
    def search_events(query: str = "", year: int = 0, limit: int = 10) -> str:
        """Search events by name, venue, city, or year."""
        return _search_events(org, query=query, year=year, limit=limit)

    @tool
    def get_event_detail(event_name: str) -> str:
        """Get full financial details for an event by name."""
        return _get_event_detail(org, event_name=event_name)

    @tool
    def get_segment_distribution() -> str:
        """Get RFM segment counts and percentages across all customers."""
        return _get_segment_distribution(org)

    @tool
    def get_top_customers(metric: str = "ltv", limit: int = 10) -> str:
        """Get top N customers by LTV or order count. metric: 'ltv' or 'orders'."""
        return _get_top_customers(org, metric=metric, limit=limit)

    @tool
    def get_revenue_by_venue() -> str:
        """Get total revenue and order count grouped by venue."""
        return _get_revenue_by_venue(org)

    @tool
    def get_repeat_customer_stats() -> str:
        """Get new vs. returning customer breakdown per event."""
        return _get_repeat_customer_stats(org)

    @tool
    def get_cohort_retention() -> str:
        """Get monthly cohort retention data showing how many customers return over time."""
        return _get_cohort_retention(org)

    @tool
    def get_upcoming_events(limit: int = 10) -> str:
        """Get future events sorted by date."""
        return _get_upcoming_events(org, limit=limit)

    @tool
    def get_knowledge_base(topic: str = "") -> str:
        """Search the knowledge base for general information."""
        return _get_knowledge_base(org, topic=topic)

    return [
        get_organization_summary,
        search_customers,
        get_customer_detail,
        search_events,
        get_event_detail,
        get_segment_distribution,
        get_top_customers,
        get_revenue_by_venue,
        get_repeat_customer_stats,
        get_cohort_retention,
        get_upcoming_events,
        get_knowledge_base,
    ]
