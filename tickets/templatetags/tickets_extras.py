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
