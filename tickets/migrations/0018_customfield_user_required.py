# Alter CustomField.user to non-null (run after 0017 backfill)

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0017_backfill_customfield_user'),
    ]

    operations = [
        migrations.AlterField(
            model_name='customfield',
            name='user',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='custom_fields',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
