from django.db import migrations, models


class Migration(migrations.Migration):
    """Rename the paid-orders tier rule to paid-events (unique events with a
    paid order). ``RenameField`` preserves the existing column data shipped by
    0185; the ``AlterField`` calls only refresh the help text so state matches
    the model.
    """

    dependencies = [
        ('tickets', '0185_loyaltytier_min_paid_orders_recent_and_more'),
    ]

    operations = [
        migrations.RenameField(
            model_name='loyaltytier',
            old_name='min_paid_orders_recent',
            new_name='min_paid_events_recent',
        ),
        migrations.RenameField(
            model_name='loyaltytier',
            old_name='paid_orders_within_days',
            new_name='paid_events_within_days',
        ),
        migrations.AlterField(
            model_name='loyaltytier',
            name='min_paid_events_recent',
            field=models.PositiveIntegerField(
                blank=True, null=True,
                help_text='Minimum number of unique events with a paid order (total > $0) '
                          'within the window below to qualify.',
            ),
        ),
        migrations.AlterField(
            model_name='loyaltytier',
            name='paid_events_within_days',
            field=models.PositiveIntegerField(
                blank=True, null=True,
                help_text='Only count paid events placed within this many days. '
                          'Leave blank to count paid events over all time.',
            ),
        ),
    ]
