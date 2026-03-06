from django.db import migrations, models


def assign_external_order_numbers(apps, schema_editor):
    """
    For all CSV-uploaded orders, copy their current order_number to
    external_order_number, then assign a fresh #NNNNN from OrderCounter.
    """
    TicketOrder = apps.get_model('tickets', 'TicketOrder')
    OrderCounter = apps.get_model('tickets', 'OrderCounter')

    csv_orders = TicketOrder.objects.filter(
        uploaded_file__isnull=False
    ).order_by('order_date', 'created_at')

    for order in csv_orders:
        order.external_order_number = order.order_number

        counter, _ = OrderCounter.objects.select_for_update().get_or_create(pk=1)
        counter.last_number += 1
        counter.save(update_fields=['last_number'])

        order.order_number = f"#{counter.last_number:05d}"
        order.save(update_fields=['order_number', 'external_order_number'])


def reverse_assign_external_order_numbers(apps, schema_editor):
    pass  # Not meaningfully reversible


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0057_global_order_counter_and_clean_order_numbers'),
    ]

    operations = [
        migrations.AddField(
            model_name='ticketorder',
            name='external_order_number',
            field=models.CharField(blank=True, db_index=True, default='', max_length=100),
            preserve_default=False,
        ),
        migrations.RunPython(
            assign_external_order_numbers,
            reverse_assign_external_order_numbers,
        ),
    ]
