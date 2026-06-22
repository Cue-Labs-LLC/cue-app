"""Built-in (system) CSV format definitions maintained by Cue.

These are stored as global ``CSVFormat`` rows (``organization=None``,
``is_system=True``) so every organization can use them out of the box without
configuring a column mapping. Orgs can "duplicate to customize" a built-in into
their own editable, org-scoped format; the global rows themselves are read-only.

``sync_builtin_formats()`` is idempotent and is invoked from a data migration so
existing and fresh databases stay current. Editing a mapping here and shipping a
new migration that calls it again upgrades the stored row in place.
"""

# Each entry mirrors the editable fields of CSVFormat. ``column_mapping`` maps an
# internal field name to the CSV column header(s) for that platform's export.
BUILTIN_CSV_FORMATS = [
    {
        "name": "POSH",
        "description": (
            "Built-in format for POSH ticketing exports, including per-ticket "
            "check-in / scan data (the \"Ticket Scan Details\" column)."
        ),
        "requires_manual_pricing": False,
        "uses_tiers": False,
        # Values are lists of candidate CSV headers (CSVProcessor.map_columns
        # iterates each field's column list).
        "column_mapping": {
            "order_number": ["Order Number"],
            "order_date": ["Order Date/Time"],
            "customer_name": ["First Name", "Last Name"],
            "customer_email": ["Email"],
            "customer_phone": ["Phone Number"],
            "ticket_type": ["Tickets Purchased"],
            "quantity": ["# of Tickets"],
            "price": ["Order Subtotal"],
            # Revenue = net to organizer (Order Subtotal). POSH's "Order Total"
            # adds the buyer-paid processing fee on top, which inflates revenue
            # relative to what POSH reports as the event's Total Revenue.
            "total_amount": ["Order Subtotal"],
            "scan_details": ["Ticket Scan Details"],
        },
    },
]


def sync_builtin_formats():
    """Create or update the global built-in CSV formats. Idempotent.

    Keyed on the (globally unique) ``name`` with ``organization=None`` and
    ``is_system=True``. Safe to call repeatedly and from migrations.
    """
    # Imported lazily so this module is import-safe before app registry is ready.
    from .models import CSVFormat

    for spec in BUILTIN_CSV_FORMATS:
        CSVFormat.objects.update_or_create(
            name=spec["name"],
            defaults={
                "organization": None,
                "is_system": True,
                "is_default": False,
                "description": spec.get("description", ""),
                "requires_manual_pricing": spec.get("requires_manual_pricing", False),
                "uses_tiers": spec.get("uses_tiers", False),
                "column_mapping": spec["column_mapping"],
            },
        )
