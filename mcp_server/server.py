"""
Cue MCP Server — exposes Cue event/customer data as MCP tools for Claude and other AI clients.

Supports two transports (set via TRANSPORT env var):
  stdio            — local process, for Claude Code / Claude Desktop stdio config (default)
  streamable-http  — HTTP server, for a hosted remote MCP URL at /mcp

Configuration (env vars):
    CUE_API_KEY   Bearer API key from Cue Settings → API Keys (cue_live_...)
    CUE_BASE_URL  Base URL of your Cue instance (default: https://cueup.co)
    TRANSPORT     "stdio" or "streamable-http" (default: stdio)
    PORT          HTTP port when using streamable-http transport (default: 8000)

Stdio setup (local, Claude Code):
    claude mcp add cue <path/to/.venv/bin/python3> <path/to/server.py> \\
      --scope user -e CUE_API_KEY=cue_live_... -e CUE_BASE_URL=https://cueup.co

Remote URL setup (after deploying to Render):
    Claude Desktop: { "mcpServers": { "cue": { "url": "https://cue-mcp.onrender.com/mcp" } } }
    Claude Code: claude mcp add cue --transport http https://cue-mcp.onrender.com/mcp --scope user
"""

import json
import os
import sys

import httpx
from mcp.server.fastmcp import FastMCP

API_KEY = os.environ.get("CUE_API_KEY", "")
BASE_URL = os.environ.get("CUE_BASE_URL", "https://cueup.co").rstrip("/")

if not API_KEY:
    print("Error: CUE_API_KEY environment variable is required.", file=sys.stderr)
    sys.exit(1)

_HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
}

mcp = FastMCP(
    "cue",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8000)),
)


def _get(path: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL}/api{path}"
    filtered = {k: v for k, v in (params or {}).items() if v is not None}
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers=_HEADERS, params=filtered)
    if resp.status_code == 404:
        return {"error": "Not found"}
    resp.raise_for_status()
    return resp.json()


def _dump(data: dict) -> str:
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def list_upcoming_events(limit: int = 10) -> str:
    """
    List events starting within the next 31 days.
    Returns event name, date, venue, ticket types with prices and availability, and talent lineup.
    """
    return _dump(_get("/v1/events/upcoming/", {"limit": limit}))


@mcp.tool()
def list_events(status: str = "all", limit: int = 20) -> str:
    """
    List all events for the organization with per-event ticket counts, revenue, expenses, and net profit.
    status: "upcoming", "past", or "all" (default: all)
    limit: max events to return (default 20, max 100)
    """
    return _dump(_get("/v1/events/", {"status": status, "limit": limit}))


@mcp.tool()
def get_event(event_id: str) -> str:
    """
    Get full details for a specific event.
    Returns attendance breakdown (new vs returning customers, check-in count),
    complete financials (ticket revenue, additional income, expenses, net profit),
    and ticket type inventory.
    event_id: UUID of the event (from list_events or list_upcoming_events)
    """
    if not event_id.strip():
        return _dump({"error": "event_id is required"})
    return _dump(_get(f"/v1/events/{event_id.strip()}/"))


@mcp.tool()
def list_customers(segment: str = "", limit: int = 50, page: int = 1) -> str:
    """
    List customers sorted by lifetime value with LTV and RFM segment info.
    segment: optional RFM segment filter — "Champions", "Loyal Customers", "Potential Loyalists",
             "At Risk", "Lost", "Need Attention" (leave blank for all)
    limit: customers per page (default 50, max 200)
    page: page number (default 1)
    """
    return _dump(_get("/v1/customers/", {
        "segment": segment or None,
        "limit": limit,
        "page": page,
    }))


@mcp.tool()
def get_customer(customer_id: str) -> str:
    """
    Get a customer's full profile.
    Returns RFM scores, behavior profile, days since last order,
    average days between orders, and their 20 most recent orders.
    customer_id: UUID of the customer (from list_customers)
    """
    if not customer_id.strip():
        return _dump({"error": "customer_id is required"})
    return _dump(_get(f"/v1/customers/{customer_id.strip()}/"))


@mcp.tool()
def get_rfm_segments() -> str:
    """
    Get the RFM segment distribution across the entire customer base.
    Returns each segment name, customer count, and percentage of total customers.
    """
    return _dump(_get("/v1/analytics/segments/"))


@mcp.tool()
def get_revenue_summary() -> str:
    """
    Get org-level revenue summary broken down by time window:
    last 30 days, last 90 days, last 365 days, and all-time.
    Covers ticket revenue and total revenue including additional income sources.
    """
    return _dump(_get("/v1/analytics/revenue/"))


@mcp.tool()
def list_orders(event_id: str = "", limit: int = 50, page: int = 1) -> str:
    """
    List recent ticket orders for the organization.
    event_id: optional UUID to filter orders to a specific event
    limit: orders per page (default 50, max 200)
    page: page number (default 1)
    """
    return _dump(_get("/v1/orders/", {
        "event_id": event_id or None,
        "limit": limit,
        "page": page,
    }))


if __name__ == "__main__":
    transport = os.environ.get("TRANSPORT", "stdio")
    mcp.run(transport=transport)
