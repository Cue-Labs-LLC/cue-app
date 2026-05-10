from datetime import timezone as py_timezone
from decimal import Decimal, InvalidOperation

from django.utils import timezone
from django.utils.dateparse import parse_datetime


def normalize_campaign_report(report: dict) -> dict:
    bounces = report.get("bounces") or {}
    opens = report.get("opens") or {}
    clicks = report.get("clicks") or {}
    ecommerce = report.get("ecommerce") or {}
    send_time = _parse_datetime(report.get("send_time"))

    return {
        "external_id": str(report.get("id") or ""),
        "campaign_title": str(report.get("campaign_title") or report.get("title") or report.get("id") or ""),
        "subject_line": str(report.get("subject_line") or ""),
        "send_time": send_time,
        "archive_url": str(report.get("archive_url") or ""),
        "emails_sent": _to_int(report.get("emails_sent")),
        "opens": _to_int(opens.get("opens_total")),
        "unique_opens": _to_int(opens.get("unique_opens")),
        "open_rate": _to_decimal(opens.get("open_rate"), "0.0000"),
        "clicks": _to_int(clicks.get("clicks_total")),
        "unique_clicks": _to_int(clicks.get("unique_clicks")),
        "click_rate": _to_decimal(clicks.get("click_rate"), "0.0000"),
        "bounces": (
            _to_int(bounces.get("hard_bounces"))
            + _to_int(bounces.get("soft_bounces"))
            + _to_int(bounces.get("syntax_errors"))
        ),
        "unsubscribes": _to_int(report.get("unsubscribed")),
        "abuse_reports": _to_int(report.get("abuse_reports")),
        "ecommerce_orders": _to_int(ecommerce.get("total_orders") or ecommerce.get("orders")),
        "ecommerce_revenue": _to_decimal(
            ecommerce.get("total_revenue") or ecommerce.get("total_spent"),
            "0.00",
        ),
        "external_metadata": {
            "type": report.get("type"),
            "list_id": report.get("list_id"),
            "list_name": report.get("list_name"),
            "raw_report": report,
        },
    }


def _parse_datetime(value):
    if not value:
        return None
    parsed = parse_datetime(str(value))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, py_timezone.utc)
    return parsed


def _to_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _to_decimal(value, default: str) -> Decimal:
    try:
        return Decimal(str(value if value is not None else default)).quantize(Decimal(default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)
