from django.db import migrations
from django.db.models import OuterRef, Subquery


def backfill_scanned_at(apps, schema_editor):
    """Stamp scanned_at on tickets whose order was already checked in.

    Before this change, check-ins were tracked only at the order level
    (TicketOrder.checked_in_at). The check-in stats now count at the ticket
    level via Ticket.scanned_at, so existing order-level check-ins must be
    propagated to their tickets or they would suddenly read as 0% attended.
    """
    Ticket = apps.get_model('tickets', 'Ticket')
    TicketOrder = apps.get_model('tickets', 'TicketOrder')
    order_checkin = TicketOrder.objects.filter(
        pk=OuterRef('ticket_order_id')
    ).values('checked_in_at')[:1]
    Ticket.objects.filter(
        scanned_at__isnull=True,
        ticket_order__checked_in_at__isnull=False,
    ).update(scanned_at=Subquery(order_checkin))


def noop_reverse(apps, schema_editor):
    # Irreversible: we can't distinguish backfilled scans from real per-ticket
    # scans, so leave the data in place on reverse.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0153_seed_builtin_csv_formats'),
    ]

    operations = [
        migrations.RunPython(backfill_scanned_at, noop_reverse),
    ]
