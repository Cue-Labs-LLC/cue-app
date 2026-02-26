from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0050_event_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='ticketorder',
            name='refunded_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
