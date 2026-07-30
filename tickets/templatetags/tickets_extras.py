from decimal import Decimal, InvalidOperation

from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def break_nbsp(value):
    """Normalize non-breaking spaces in trusted rich-text HTML to regular spaces.

    Descriptions pasted from rich-text editors (Notes, Docs, Word) often join
    every word with a non-breaking space. That leaves no line-break opportunity,
    so the browser treats a whole paragraph as one long token and breaks it
    mid-word (e.g. "soun ds", "Af ro-house"). Swapping &nbsp;/U+00A0 for regular
    spaces restores normal word wrapping. Returns safe HTML (the description is
    already trusted, so this replaces the previous |safe usage)."""
    if not value:
        return value
    text = str(value).replace(' ', ' ').replace('&nbsp;', ' ')
    return mark_safe(text)


@register.filter
def get_item(d, key):
    """Look up key in dict; return 'secondary' if missing (for segment badge color)."""
    if d is None:
        return "secondary"
    return d.get(key, "secondary")


@register.filter
def getlist(querydict, key):
    """Return querydict.getlist(key) ([] if absent / not a QueryDict).
    Used to re-check survey inputs from request.POST on a re-rendered form."""
    try:
        return querydict.getlist(key)
    except AttributeError:
        return []


# Single source of truth for the CustomerTag / LoyaltyTier color -> Bootstrap
# badge class mapping (the two share the same 6-color choice set).
_BADGE_CLASSES = {
    'blue': 'bg-primary',
    'green': 'bg-success',
    'red': 'bg-danger',
    'yellow': 'bg-warning text-dark',
    'orange': 'bg-warning text-dark',
    'purple': 'badge-purple',
}


@register.filter
def badge_class(color):
    """Map a tag/tier color name to its Bootstrap badge class."""
    return _BADGE_CLASSES.get(color, 'bg-secondary')


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
def filter_channel(rows, channel):
    """Filter a list of campaign-row dicts by `channel` key."""
    if not rows:
        return []
    try:
        return [row for row in rows if row.get('channel') == channel]
    except AttributeError:
        return []


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


@register.filter
def cents(value):
    """Format integer cents as a dollar amount: 1050 → '10.50'. None/'' → '0.00'."""
    if value is None or value == '':
        return '0.00'
    try:
        return f"{Decimal(str(value)) / 100:,.2f}"
    except (TypeError, ValueError, InvalidOperation):
        return '0.00'


@register.filter
def tokens(value):
    """Format a cents amount as a token count (1 token = 1 SMS segment).

    floor(cents / SMS_PRICE_PER_SEGMENT_CENTS) — floor (not int(), which truncates
    toward zero and would render a −1¢ debit row as '0'). Clean integer counts assume
    a whole-cent per-segment price; the underlying money stays in cents."""
    import math
    from django.conf import settings as _settings
    if value is None or value == '':
        return 0
    try:
        price = Decimal(str(getattr(_settings, 'SMS_PRICE_PER_SEGMENT_CENTS', 3)))
        return math.floor(Decimal(str(value)) / price)
    except (TypeError, ValueError, InvalidOperation, ZeroDivisionError):
        return 0


@register.filter
def survey_value(value):
    """Render a Typeform answer value as plain text. Lists become comma-joined
    strings (no quotes, no brackets); everything else stringifies normally.
    Example: ['Afrobeats', 'Dancehall'] → 'Afrobeats, Dancehall'."""
    if value is None:
        return ''
    if isinstance(value, (list, tuple)):
        return ', '.join(str(v) for v in value if v not in (None, ''))
    return str(value)
