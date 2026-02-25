from django.db import migrations, models


def set_existing_events_live(apps, schema_editor):
    apps.get_model('tickets', 'Event').objects.all().update(status='live')


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0049_add_platform_fee_cents_to_stripe_session'),
    ]

    operations = [
        migrations.AddField(
            model_name='event',
            name='status',
            field=models.CharField(
                choices=[('draft', 'Draft'), ('live', 'Live'), ('ended', 'Ended')],
                default='draft',
                db_index=True,
                help_text='Publication state; only meaningful for direct ticketing events.',
                max_length=10,
            ),
        ),
        migrations.RunPython(set_existing_events_live, reverse_code=noop),
        migrations.AddIndex(
            model_name='event',
            index=models.Index(fields=['ticketing_type', 'status', 'start_date'], name='tickets_eve_ticketi_idx'),
        ),
    ]
