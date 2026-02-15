from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0006_add_event_capacity'),
    ]

    operations = [
        migrations.AddField(
            model_name='event',
            name='start_date',
            field=models.DateField(db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='event',
            name='start_time',
            field=models.TimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='event',
            name='end_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='event',
            name='end_time',
            field=models.TimeField(blank=True, null=True),
        ),
    ]
