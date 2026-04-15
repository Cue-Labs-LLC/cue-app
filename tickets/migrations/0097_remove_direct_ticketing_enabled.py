from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0096_backfill_organization_membership'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='featureflagsettings',
            name='direct_ticketing_enabled',
        ),
    ]
