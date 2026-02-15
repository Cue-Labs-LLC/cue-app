from django import template

register = template.Library()


@register.filter
def get_item(d, key):
    """Look up key in dict; return 'secondary' if missing (for segment badge color)."""
    if d is None:
        return "secondary"
    return d.get(key, "secondary")
