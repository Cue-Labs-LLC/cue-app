"""
Cue MCP Server — exposes Cue event/customer data as MCP tools for Claude and other AI clients.

Configuration (env vars):
    CUE_API_KEY   Bearer API key from Cue (Settings → API Keys → cue_live_...)
    CUE_BASE_URL  Base URL of your Cue instance (default: https://cueup.co)

Claude Desktop setup (~/.config/claude/claude_desktop_config.json):
    {
      "mcpServers": {
        "cue": {
          "command": "python",
          "args": ["/path/to/mcp_server/server.py"],
          "env": {
            "CUE_API_KEY": "cue_live_your_key_here",
            "CUE_BASE_URL": "https://cueup.co"
          }
        }
      }
    }
"""

import os
import sys
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

API_KEY = os.environ.get("CUE_API_KEY", "")
BASE_URL = os.environ.get("CUE_BASE_URL", "https://cueup.co").rstrip("/")

if not API_KEY:
    print("Error: CUE_API_KEY environment variable is required.", file=sys.stderr)
    sys.exit(1)

_HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
}

server = Server("cue")


def _get(path: str, params: dict | None = None) -> Any:
    """Make an authenticated GET request to the Cue API."""
    url = f"{BASE_URL}/api{path}"
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers=_HEADERS, params={k: v for k, v in (params or {}).items() if v is not None})
    if resp.status_code == 404:
        return {"error": "Not found"}
    resp.raise_for_status()
    return resp.json()


def _text(data: Any) -> list[TextContent]:
    import json
    return [TextContent(type="text", text=json.dumps(data, indent=2, default=str))]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="list_upcoming_events",
            description=(
                "List events starting within the next 31 days for your Cue organization. "
                "Returns event name, date, venue, ticket types (with prices and availability), and lineup."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of events to return (default 10, max 50)",
                        "default": 10,
                    }
                },
            },
        ),
        Tool(
            name="list_events",
            description=(
                "List all events for your Cue organization with per-event ticket counts, revenue, "
                "expenses, and net profit. Filter by status to narrow results."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["upcoming", "past", "all"],
                        "description": "Filter events by timing relative to today (default: all)",
                        "default": "all",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of events to return (default 20, max 100)",
                        "default": 20,
                    },
                },
            },
        ),
        Tool(
            name="get_event",
            description=(
                "Get full details for a specific event including attendance breakdown (new vs returning "
                "customers, check-in count), complete financials (ticket revenue, additional income, "
                "expenses, net profit), and ticket type inventory."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "UUID of the event (from list_events or list_upcoming_events)",
                    }
                },
                "required": ["event_id"],
            },
        ),
        Tool(
            name="list_customers",
            description=(
                "List customers for your Cue organization sorted by lifetime value. "
                "Includes LTV, RFM segment (Champions, Loyal Customers, At Risk, etc.), "
                "order count, and last purchase date. Optionally filter by RFM segment."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "segment": {
                        "type": "string",
                        "description": (
                            "Filter by RFM segment name, e.g. 'Champions', 'Loyal Customers', "
                            "'Potential Loyalists', 'At Risk', 'Lost', 'Need Attention'"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of customers per page (default 50, max 200)",
                        "default": 50,
                    },
                    "page": {
                        "type": "integer",
                        "description": "Page number (default 1)",
                        "default": 1,
                    },
                },
            },
        ),
        Tool(
            name="get_customer",
            description=(
                "Get a customer's full profile including RFM scores, behavior profile, "
                "days since last order, average days between orders, and their 20 most recent orders."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "customer_id": {
                        "type": "string",
                        "description": "UUID of the customer (from list_customers)",
                    }
                },
                "required": ["customer_id"],
            },
        ),
        Tool(
            name="get_rfm_segments",
            description=(
                "Get the RFM segment distribution across your entire customer base. "
                "Returns each segment name, customer count, and percentage of total customers."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_revenue_summary",
            description=(
                "Get an org-level revenue summary broken down by time window: "
                "last 30 days, last 90 days, last 365 days, and all-time. "
                "Covers ticket revenue and total revenue including additional income sources."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="list_orders",
            description=(
                "List recent ticket orders for your organization. "
                "Optionally filter to a specific event by event_id. "
                "Returns customer info, event info, order amount, and check-in/refund status."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "Optional UUID of an event to filter orders by",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of orders per page (default 50, max 200)",
                        "default": 50,
                    },
                    "page": {
                        "type": "integer",
                        "description": "Page number (default 1)",
                        "default": 1,
                    },
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "list_upcoming_events":
        data = _get("/v1/events/upcoming/", {"limit": arguments.get("limit")})
        return _text(data)

    if name == "list_events":
        data = _get("/v1/events/", {
            "status": arguments.get("status"),
            "limit": arguments.get("limit"),
        })
        return _text(data)

    if name == "get_event":
        event_id = arguments.get("event_id", "").strip()
        if not event_id:
            return _text({"error": "event_id is required"})
        data = _get(f"/v1/events/{event_id}/")
        return _text(data)

    if name == "list_customers":
        data = _get("/v1/customers/", {
            "segment": arguments.get("segment"),
            "limit": arguments.get("limit"),
            "page": arguments.get("page"),
        })
        return _text(data)

    if name == "get_customer":
        customer_id = arguments.get("customer_id", "").strip()
        if not customer_id:
            return _text({"error": "customer_id is required"})
        data = _get(f"/v1/customers/{customer_id}/")
        return _text(data)

    if name == "get_rfm_segments":
        data = _get("/v1/analytics/segments/")
        return _text(data)

    if name == "get_revenue_summary":
        data = _get("/v1/analytics/revenue/")
        return _text(data)

    if name == "list_orders":
        data = _get("/v1/orders/", {
            "event_id": arguments.get("event_id"),
            "limit": arguments.get("limit"),
            "page": arguments.get("page"),
        })
        return _text(data)

    return _text({"error": f"Unknown tool: {name}"})


async def main():
    async with stdio_server() as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
