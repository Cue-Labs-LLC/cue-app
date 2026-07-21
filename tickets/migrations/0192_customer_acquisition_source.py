from django.db import migrations, models


def backfill_acquisition_source(apps, schema_editor):
    """Attribute legacy customers created before this field existed.

    Only phone-only opted-in rows are provably from the public opt-in form; every
    other existing contact is treated as an Import, since CSV import was the org's
    historic ingest path. New rows are stamped at creation, so this only touches
    the blank backlog.
    """
    Customer = apps.get_model('tickets', 'Customer')
    # Provable subscribers: phone-only rows (no email) that opted in via the form.
    Customer.objects.filter(
        acquisition_source='', email='', sms_opt_in=True,
    ).exclude(phone='').update(acquisition_source='subscribe_form')
    # Everyone else still blank → Import.
    Customer.objects.filter(acquisition_source='').update(acquisition_source='import')


def noop_reverse(apps, schema_editor):
    # Nothing to undo — the column is dropped by the reverse of AddField.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0191_organization_sms_subscribe_title'),
    ]

    operations = [
        migrations.AddField(
            model_name='customer',
            name='acquisition_source',
            field=models.CharField(blank=True, choices=[('subscribe_form', 'Opt-in form'), ('ticket_purchase', 'Ticket purchase'), ('import', 'Import'), ('manual', 'Manual')], db_index=True, help_text='How this customer first entered the org. Set once at creation; immutable.', max_length=20),
        ),
        migrations.RunPython(backfill_acquisition_source, noop_reverse),
    ]
