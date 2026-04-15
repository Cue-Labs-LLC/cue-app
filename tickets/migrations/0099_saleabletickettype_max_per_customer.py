from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0098_event_max_tickets_per_customer'),
    ]

    operations = [
        migrations.AddField(
            model_name='saleabletickettype',
            name='max_per_customer',
            field=models.PositiveIntegerField(
                blank=True,
                help_text='Max tickets a single customer can purchase for this ticket type; null = unlimited',
                null=True,
            ),
        ),
    ]
