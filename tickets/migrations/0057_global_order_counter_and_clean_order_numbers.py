from django.db import migrations, models


def assign_clean_order_numbers(apps, schema_editor):
    TicketOrder = apps.get_model('tickets', 'TicketOrder')
    OrderCounter = apps.get_model('tickets', 'OrderCounter')

    direct_orders = TicketOrder.objects.filter(
        uploaded_file__isnull=True
    ).order_by('order_date')

    for i, order in enumerate(direct_orders, start=1):
        order.order_number = f"#{i:05d}"
        order.save(update_fields=['order_number'])

    if direct_orders.exists():
        counter, _ = OrderCounter.objects.get_or_create(pk=1)
        counter.last_number = direct_orders.count()
        counter.save()


def reverse_assign_order_numbers(apps, schema_editor):
    pass  # Not reversible in a meaningful way


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0056_add_order_counter_and_display_order_number'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='ticketorder',
            name='display_order_number',
        ),
        migrations.RemoveField(
            model_name='ordercounter',
            name='organization',
        ),
        migrations.RunPython(
            assign_clean_order_numbers,
            reverse_assign_order_numbers,
        ),
    ]
