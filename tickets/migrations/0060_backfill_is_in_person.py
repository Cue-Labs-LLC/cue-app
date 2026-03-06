from django.db import migrations


def backfill_is_in_person(apps, schema_editor):
    TicketOrder = apps.get_model('tickets', 'TicketOrder')
    TicketOrder.objects.filter(
        customer__email__endswith='@placeholder.local'
    ).update(is_in_person=True)


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0059_ticketorder_is_in_person'),
    ]

    operations = [
        migrations.RunPython(backfill_is_in_person, migrations.RunPython.noop),
    ]
