from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0073_add_scanner_pin_and_session'),
    ]

    operations = [
        migrations.AddField(
            model_name='event',
            name='public_id',
            field=models.CharField(blank=True, db_index=True, max_length=10, null=True),
        ),
    ]
