from django.db import migrations


def backfill_origin(apps, schema_editor):
    """Every pre-cutover payout that drew platform funds created a Transfer
    first, so a stored transfer id is the reliable historical marker. Rows
    without one (failed before the Transfer call) stay on the 'cue' default,
    which is correct: FAILED rows are excluded from all pool sums anyway.
    """
    Payout = apps.get_model('tickets', 'Payout')
    Payout.objects.filter(stripe_transfer_id__isnull=False).update(origin='legacy_transfer')


def reverse_origin(apps, schema_editor):
    Payout = apps.get_model('tickets', 'Payout')
    Payout.objects.update(origin='cue')


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0148_payout_origin_stripecheckoutsession_charge_flow_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_origin, reverse_origin),
    ]
