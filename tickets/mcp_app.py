"""
Embedded MCP server for Cue — served at /mcp on the main Django app.

Any org connects by pointing their MCP client at https://cueup.co/mcp and passing
their API key as 'Authorization: Bearer cue_live_...' in the request headers.

Compatible with Claude Desktop, Claude Code, and any MCP-compatible client.
"""
import contextvars
import json
import logging
from datetime import timedelta
from decimal import Decimal

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db.models import Count, DecimalField, IntegerField, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .models import (
    Customer,
    Event,
    EventCustomFieldValue,
    EventExpense,
    EventIncome,
    OAuthAccessToken,
    OrganizationAPIKey,
    SaleableTicketType,
    TicketOrder,
)

logger = logging.getLogger(__name__)

_current_org: contextvars.ContextVar = contextvars.ContextVar("mcp_org", default=None)

mcp = FastMCP(
    "cue",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    stateless_http=True,
)


def _dump(data) -> str:
    return json.dumps(data, indent=2, default=str)


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

class OrgAuthMiddleware:
    """Pure ASGI middleware — validates Bearer API key and injects org into ContextVar."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        site_url = getattr(settings, 'SITE_URL', 'https://cueup.co').rstrip('/')
        www_auth = f'Bearer resource_metadata="{site_url}/.well-known/oauth-protected-resource"'

        headers = {k.lower().decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")

        if not auth.lower().startswith("bearer "):
            await self._reject(send, 401, "Authorization header required", www_auth)
            return

        raw_token = auth[7:]

        if raw_token.startswith("cue_live_"):
            try:
                api_key = await sync_to_async(
                    lambda: OrganizationAPIKey.objects.select_related("organization").get(
                        key=raw_token, is_active=True
                    )
                )()
            except OrganizationAPIKey.DoesNotExist:
                await self._reject(send, 401, "Invalid or revoked API key", www_auth)
                return
            except Exception:
                logger.exception("MCP auth lookup failed")
                await self._reject(send, 500, "Authentication error")
                return
            await sync_to_async(
                lambda: OrganizationAPIKey.objects.filter(pk=api_key.pk).update(
                    last_used_at=timezone.now()
                )
            )()
            org = api_key.organization

        elif raw_token.startswith("cue_at_"):
            try:
                access_token = await sync_to_async(
                    lambda: OAuthAccessToken.objects.select_related("organization").get(
                        token=raw_token, expires_at__gt=timezone.now()
                    )
                )()
            except OAuthAccessToken.DoesNotExist:
                await self._reject(send, 401, "Invalid or expired access token", www_auth)
                return
            except Exception:
                logger.exception("MCP OAuth token lookup failed")
                await self._reject(send, 500, "Authentication error")
                return
            org = access_token.organization

        else:
            await self._reject(send, 401, "Unrecognized token format", www_auth)
            return

        token = _current_org.set(org)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_org.reset(token)

    @staticmethod
    async def _reject(send, status: int, message: str, www_authenticate: str | None = None):
        body = json.dumps({"error": message}).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        if www_authenticate:
            headers.append((b"www-authenticate", www_authenticate.encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_upcoming_events(limit: int = 10) -> str:
    """
    List events starting within the next 31 days.
    Returns event name, date, venue, ticket types with prices and availability, and talent lineup.
    """
    from django.db.models import Prefetch

    org = _current_org.get()
    limit = min(limit, 50)
    today = timezone.localdate()
    cutoff = today + timedelta(days=31)

    events = await sync_to_async(list)(
        Event.objects
        .filter(organization=org, start_date__gte=today, start_date__lte=cutoff, deleted_at__isnull=True)
        .select_related("venue")
        .prefetch_related(
            "talent_lineup",
            Prefetch(
                "custom_field_values",
                queryset=EventCustomFieldValue.objects.select_related("custom_field", "custom_field_option"),
            ),
            Prefetch(
                "saleable_ticket_types",
                queryset=SaleableTicketType.objects.filter(is_active=True).order_by("order", "name"),
            ),
        )
        .order_by("start_date", "start_time", "name")[:limit]
    )

    def _build(event):
        venue = event.venue
        custom_fields = {
            cfv.custom_field.name: cfv.custom_field_option.label
            for cfv in event.custom_field_values.all()
            if cfv.custom_field_option
        }
        return {
            "name": event.name,
            "summary": event.summary,
            "description": event.description,
            "start_date": event.start_date.isoformat(),
            "start_time": event.start_time.isoformat() if event.start_time else None,
            "end_date": event.end_date.isoformat() if event.end_date else None,
            "end_time": event.end_time.isoformat() if event.end_time else None,
            "timezone": event.timezone,
            "ticket_link": event.ticket_link or None,
            "venue": {
                "name": venue.name,
                "city": venue.city,
                "street_address": venue.street_address,
                "state": venue.state,
                "postal_code": venue.postal_code,
                "country": venue.country,
            },
            "talent_lineup": [t.name for t in event.talent_lineup.all()],
            "additional_details": custom_fields,
            "ticket_types": [
                {
                    "name": tt.name,
                    "price": str(tt.price),
                    "description": tt.description,
                    "sold_out": tt.remaining_quantity() == 0,
                }
                for tt in event.saleable_ticket_types.all()
            ],
        }

    data = [_build(e) for e in events]
    return _dump({
        "organization": org.name,
        "generated_at": timezone.now().isoformat(),
        "event_count": len(data),
        "events": data,
    })


@mcp.tool()
async def list_events(status: str = "all", limit: int = 20) -> str:
    """
    List all events with per-event ticket counts, revenue, expenses, and net profit.
    status: "upcoming", "past", or "all" (default: all)
    limit: max events to return (default 20, max 100)
    """
    org = _current_org.get()
    today = timezone.localdate()
    limit = min(limit, 100)

    orders_sq = TicketOrder.objects.filter(event=OuterRef("pk"), refunded_at__isnull=True).values("event").annotate(c=Count("id")).values("c")
    revenue_sq = TicketOrder.objects.filter(event=OuterRef("pk"), refunded_at__isnull=True).values("event").annotate(s=Sum("total_amount")).values("s")
    expense_sq = EventExpense.objects.filter(event=OuterRef("pk"), deleted_at__isnull=True).values("event").annotate(s=Sum("amount")).values("s")
    income_sq = EventIncome.objects.filter(event=OuterRef("pk"), deleted_at__isnull=True).values("event").annotate(s=Sum("amount")).values("s")

    qs = Event.objects.filter(organization=org, deleted_at__isnull=True)
    if status == "upcoming":
        qs = qs.filter(start_date__gte=today)
    elif status == "past":
        qs = qs.filter(start_date__lt=today)

    events = await sync_to_async(list)(
        qs.select_related("venue")
        .annotate(
            ticket_count=Coalesce(Subquery(orders_sq, output_field=IntegerField()), 0),
            ticket_revenue=Coalesce(Subquery(revenue_sq, output_field=DecimalField(max_digits=12, decimal_places=2)), Decimal("0.00")),
            total_expenses=Coalesce(Subquery(expense_sq, output_field=DecimalField(max_digits=12, decimal_places=2)), Decimal("0.00")),
            additional_income_total=Coalesce(Subquery(income_sq, output_field=DecimalField(max_digits=12, decimal_places=2)), Decimal("0.00")),
        )
        .order_by("-start_date", "-start_time")[:limit]
    )

    data = []
    for event in events:
        venue = event.venue
        total_revenue = event.ticket_revenue + event.additional_income_total
        data.append({
            "id": str(event.id),
            "name": event.name,
            "summary": event.summary,
            "start_date": event.start_date.isoformat(),
            "start_time": event.start_time.isoformat() if event.start_time else None,
            "end_date": event.end_date.isoformat() if event.end_date else None,
            "status": "upcoming" if event.start_date >= today else "past",
            "venue": {"name": venue.name, "city": venue.city, "state": venue.state},
            "tickets_sold": event.ticket_count,
            "ticket_revenue": str(event.ticket_revenue),
            "additional_income": str(event.additional_income_total),
            "total_revenue": str(total_revenue),
            "total_expenses": str(event.total_expenses),
            "net_profit": str(total_revenue - event.total_expenses),
        })

    return _dump({
        "organization": org.name,
        "generated_at": timezone.now().isoformat(),
        "event_count": len(data),
        "events": data,
    })


@mcp.tool()
async def get_event(event_id: str) -> str:
    """
    Get full details for a specific event: attendance breakdown, financials, and ticket types.
    event_id: UUID of the event (from list_events or list_upcoming_events)
    """
    org = _current_org.get()
    if not event_id.strip():
        return _dump({"error": "event_id is required"})

    today = timezone.localdate()

    try:
        event = await sync_to_async(
            lambda: Event.objects.filter(organization=org, deleted_at__isnull=True)
            .select_related("venue").get(id=event_id.strip())
        )()
    except Event.DoesNotExist:
        return _dump({"error": "Event not found"})
    except Exception:
        return _dump({"error": "Invalid event_id"})

    venue = event.venue
    orders_qs = TicketOrder.objects.filter(event=event, refunded_at__isnull=True)

    agg = await sync_to_async(
        lambda: orders_qs.aggregate(
            total_orders=Count("id"),
            checked_in=Count("id", filter=Q(checked_in_at__isnull=False)),
            ticket_revenue=Coalesce(Sum("total_amount"), Decimal("0.00")),
        )
    )()

    attended_ids = await sync_to_async(lambda: set(orders_qs.values_list("customer_id", flat=True)))()
    prior_ids = await sync_to_async(
        lambda: set(
            TicketOrder.objects
            .filter(customer_id__in=attended_ids, event__organization=org, refunded_at__isnull=True)
            .exclude(event=event)
            .values_list("customer_id", flat=True)
            .distinct()
        )
    )()

    expenses = await sync_to_async(list)(
        EventExpense.objects.filter(event=event, deleted_at__isnull=True)
        .values("category", "description", "amount", "expense_date")
        .order_by("-expense_date")
    )
    income_lines = await sync_to_async(list)(
        EventIncome.objects.filter(event=event, deleted_at__isnull=True)
        .select_related("income_source").order_by("income_source__order")
    )
    ticket_types = await sync_to_async(list)(
        event.saleable_ticket_types.filter(is_active=True).order_by("order", "name")
    )

    total_expenses = sum(e["amount"] for e in expenses)
    total_additional_income = sum(i.amount for i in income_lines)
    ticket_revenue = agg["ticket_revenue"]
    total_revenue = ticket_revenue + total_additional_income

    return _dump({
        "id": str(event.id),
        "name": event.name,
        "summary": event.summary,
        "description": event.description,
        "start_date": event.start_date.isoformat(),
        "start_time": event.start_time.isoformat() if event.start_time else None,
        "end_date": event.end_date.isoformat() if event.end_date else None,
        "end_time": event.end_time.isoformat() if event.end_time else None,
        "timezone": event.timezone,
        "status": "upcoming" if event.start_date >= today else "past",
        "venue": {
            "name": venue.name,
            "city": venue.city,
            "state": venue.state,
            "street_address": venue.street_address,
            "postal_code": venue.postal_code,
            "country": venue.country,
            "capacity": venue.capacity,
        },
        "attendance": {
            "total_orders": agg["total_orders"],
            "checked_in": agg["checked_in"],
            "new_customers": len(attended_ids - prior_ids),
            "returning_customers": len(prior_ids & attended_ids),
        },
        "financials": {
            "ticket_revenue": str(ticket_revenue),
            "additional_income": str(total_additional_income),
            "total_revenue": str(total_revenue),
            "total_expenses": str(total_expenses),
            "net_profit": str(total_revenue - total_expenses),
            "income": [
                {"source": i.income_source.name, "amount": str(i.amount), "date": i.income_date.isoformat() if i.income_date else None}
                for i in income_lines
            ],
            "expenses": [
                {"category": e["category"], "description": e["description"], "amount": str(e["amount"]), "date": e["expense_date"].isoformat() if e["expense_date"] else None}
                for e in expenses
            ],
        },
        "ticket_types": [
            {"name": tt.name, "price": str(tt.price), "quantity_limit": tt.quantity_limit, "quantity_sold": tt.quantity_sold, "remaining": tt.remaining_quantity()}
            for tt in ticket_types
        ],
    })


@mcp.tool()
async def list_customers(segment: str = "", limit: int = 50, page: int = 1) -> str:
    """
    List customers sorted by lifetime value with LTV and RFM segment info.
    segment: optional RFM segment filter — "Champions", "Loyal Customers", "Potential Loyalists",
             "At Risk", "Lost", "Need Attention" (leave blank for all)
    limit: customers per page (default 50, max 200)
    page: page number (default 1)
    """
    org = _current_org.get()
    limit = min(limit, 200)
    page = max(page, 1)

    order_count_sq = (
        TicketOrder.objects.filter(customer=OuterRef("pk"), refunded_at__isnull=True)
        .values("customer").annotate(c=Count("id")).values("c")
    )

    qs = Customer.objects.filter(organization=org)
    if segment.strip():
        qs = qs.filter(rfm_segment=segment.strip())

    qs = qs.annotate(
        order_count=Coalesce(Subquery(order_count_sq, output_field=IntegerField()), 0),
    ).order_by("-lifetime_value")

    total = await sync_to_async(qs.count)()
    offset = (page - 1) * limit
    customers = await sync_to_async(list)(qs[offset: offset + limit])

    return _dump({
        "organization": org.name,
        "generated_at": timezone.now().isoformat(),
        "total": total,
        "page": page,
        "limit": limit,
        "customers": [
            {
                "id": str(c.id),
                "email": c.email,
                "name": c.name,
                "lifetime_value": str(c.lifetime_value),
                "rfm_segment": c.rfm_segment or None,
                "behavior_profile": c.behavior_profile or None,
                "order_count": c.order_count,
                "last_order_date": c.last_order_date.isoformat() if c.last_order_date else None,
            }
            for c in customers
        ],
    })


@mcp.tool()
async def get_customer(customer_id: str) -> str:
    """
    Get a customer's full profile: RFM scores, behavior, days since last order, and recent orders.
    customer_id: UUID of the customer (from list_customers)
    """
    org = _current_org.get()
    if not customer_id.strip():
        return _dump({"error": "customer_id is required"})

    try:
        customer = await sync_to_async(
            lambda: Customer.objects.filter(organization=org).get(id=customer_id.strip())
        )()
    except Customer.DoesNotExist:
        return _dump({"error": "Customer not found"})
    except Exception:
        return _dump({"error": "Invalid customer_id"})

    orders = await sync_to_async(list)(
        TicketOrder.objects.filter(customer=customer)
        .select_related("event", "event__venue")
        .order_by("-order_date")[:20]
    )

    return _dump({
        "id": str(customer.id),
        "email": customer.email,
        "name": customer.name,
        "phone": customer.phone or None,
        "lifetime_value": str(customer.lifetime_value),
        "rfm_segment": customer.rfm_segment or None,
        "rfm_recency_score": customer.rfm_recency_score,
        "rfm_frequency_score": customer.rfm_frequency_score,
        "rfm_monetary_score": customer.rfm_monetary_score,
        "behavior_profile": customer.behavior_profile or None,
        "days_since_last_order": customer.days_since_last_order,
        "avg_days_between_orders": customer.avg_days_between_orders,
        "last_order_date": customer.last_order_date.isoformat() if customer.last_order_date else None,
        "sms_opt_in": customer.sms_opt_in,
        "recent_orders": [
            {
                "id": str(o.id),
                "order_number": o.display_order_number,
                "event": o.event.name,
                "event_date": o.event.start_date.isoformat(),
                "order_date": o.order_date.isoformat(),
                "total_amount": str(o.total_amount),
                "refunded": o.refunded_at is not None,
                "checked_in": o.checked_in_at is not None,
            }
            for o in orders
        ],
    })


@mcp.tool()
async def get_rfm_segments() -> str:
    """
    Get the RFM segment distribution across the entire customer base.
    Returns each segment name, customer count, and percentage of total customers.
    """
    org = _current_org.get()
    all_customers = Customer.objects.filter(organization=org)

    total = await sync_to_async(all_customers.count)()
    segments = await sync_to_async(list)(
        all_customers.exclude(rfm_segment="").values("rfm_segment").annotate(count=Count("id")).order_by("-count")
    )
    unscored = await sync_to_async(lambda: all_customers.filter(rfm_segment="").count())()

    return _dump({
        "organization": org.name,
        "generated_at": timezone.now().isoformat(),
        "total_customers": total,
        "unscored_customers": unscored,
        "segments": [
            {"segment": s["rfm_segment"], "count": s["count"], "pct": round(s["count"] / total * 100, 1) if total else 0}
            for s in segments
        ],
    })


@mcp.tool()
async def get_revenue_summary() -> str:
    """
    Get org-level revenue summary broken down by time window:
    last 30 days, last 90 days, last 365 days, and all-time.
    Covers ticket revenue and total revenue including additional income sources.
    """
    org = _current_org.get()
    now = timezone.now()
    base_qs = TicketOrder.objects.filter(customer__organization=org, refunded_at__isnull=True)

    async def _revenue(days=None):
        qs = base_qs.filter(order_date__gte=now - timedelta(days=days)) if days else base_qs
        return await sync_to_async(lambda: qs.aggregate(s=Coalesce(Sum("total_amount"), Decimal("0.00")))["s"])()

    event_count = await sync_to_async(
        lambda: Event.objects.filter(organization=org, deleted_at__isnull=True).count()
    )()
    additional_income = await sync_to_async(
        lambda: EventIncome.objects.filter(event__organization=org, deleted_at__isnull=True)
        .aggregate(s=Coalesce(Sum("amount"), Decimal("0.00")))["s"]
    )()

    ticket_all = await _revenue()

    return _dump({
        "organization": org.name,
        "generated_at": timezone.now().isoformat(),
        "event_count": event_count,
        "ticket_revenue": {
            "last_30_days": str(await _revenue(30)),
            "last_90_days": str(await _revenue(90)),
            "last_365_days": str(await _revenue(365)),
            "all_time": str(ticket_all),
        },
        "total_revenue_all_time": str(ticket_all + additional_income),
    })


@mcp.tool()
async def list_orders(event_id: str = "", limit: int = 50, page: int = 1) -> str:
    """
    List recent ticket orders for the organization.
    event_id: optional UUID to filter orders to a specific event
    limit: orders per page (default 50, max 200)
    page: page number (default 1)
    """
    org = _current_org.get()
    limit = min(limit, 200)
    page = max(page, 1)

    qs = (
        TicketOrder.objects.filter(customer__organization=org)
        .select_related("customer", "event", "event__venue")
        .order_by("-order_date")
    )
    if event_id.strip():
        qs = qs.filter(event_id=event_id.strip())

    total = await sync_to_async(qs.count)()
    offset = (page - 1) * limit
    orders = await sync_to_async(list)(qs[offset: offset + limit])

    return _dump({
        "organization": org.name,
        "generated_at": timezone.now().isoformat(),
        "total": total,
        "page": page,
        "limit": limit,
        "orders": [
            {
                "id": str(o.id),
                "order_number": o.display_order_number,
                "order_date": o.order_date.isoformat(),
                "customer": {"id": str(o.customer.id), "name": o.customer.name, "email": o.customer.email},
                "event": {"id": str(o.event.id), "name": o.event.name, "start_date": o.event.start_date.isoformat(), "venue_city": o.event.venue.city},
                "total_amount": str(o.total_amount),
                "refunded": o.refunded_at is not None,
                "checked_in": o.checked_in_at is not None,
            }
            for o in orders
        ],
    })


def build_mcp_app():
    """Return the FastMCP Starlette app wrapped with org auth middleware."""
    return OrgAuthMiddleware(mcp.streamable_http_app())
