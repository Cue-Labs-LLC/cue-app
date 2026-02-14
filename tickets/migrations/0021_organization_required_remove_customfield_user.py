# Make organization required on all org-scoped models; remove CustomField.user

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0020_backfill_organization'),
    ]

    operations = [
        migrations.AlterField(
            model_name='csvformat',
            name='organization',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='csv_formats',
                to='tickets.organization',
            ),
        ),
        migrations.AlterField(
            model_name='customfield',
            name='organization',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='custom_fields',
                to='tickets.organization',
            ),
        ),
        migrations.AlterField(
            model_name='customer',
            name='organization',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='customers',
                to='tickets.organization',
            ),
        ),
        migrations.AlterField(
            model_name='event',
            name='organization',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='events',
                to='tickets.organization',
            ),
        ),
        migrations.AlterField(
            model_name='uploadedfile',
            name='organization',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='uploaded_files',
                to='tickets.organization',
            ),
        ),
        migrations.AlterField(
            model_name='venue',
            name='organization',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='venues',
                to='tickets.organization',
            ),
        ),
        migrations.RemoveField(
            model_name='customfield',
            name='user',
        ),
    ]
