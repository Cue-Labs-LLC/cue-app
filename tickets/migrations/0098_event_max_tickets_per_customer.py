from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0097_remove_direct_ticketing_enabled'),
    ]

    operations = [
        migrations.AddField(
            model_name='event',
            name='max_tickets_per_customer',
            field=models.PositiveIntegerField(
                blank=True,
                help_text='Optional cumulative cap on how many tickets one customer can buy for this event.',
                null=True,
            ),
        ),
    ]
