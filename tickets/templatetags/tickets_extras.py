from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def get_item(d, key):
    """Look up key in dict; return 'secondary' if missing (for segment badge color)."""
    if d is None:
        return "secondary"
    return d.get(key, "secondary")


@register.filter
def subtract(value, arg):
    """Subtract arg from value (Decimal-safe)."""
    try:
        return Decimal(str(value)) - Decimal(str(arg))
    except (TypeError, ValueError, InvalidOperation):
        return ''


@register.filter
def make_range(value):
    """Return range(value) for iteration in templates."""
    try:
        return range(int(value))
    except (TypeError, ValueError):
        return range(0)


@register.filter
def intcomma(value):
    """Format an integer with comma thousands separators (e.g. 1234 -> '1,234')."""
    if value is None or value == '':
        return '0'
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


@register.filter
def currency(value):
    """Format a numeric value with comma thousands separator and 2 decimal places.
    Example: 1234567.8 → '1,234,567.80'. Returns '0.00' for None/empty."""
    if value is None or value == '':
        return '0.00'
    try:
        return f"{Decimal(str(value)):,.2f}"
    except (TypeError, ValueError, InvalidOperation):
        return '0.00'
