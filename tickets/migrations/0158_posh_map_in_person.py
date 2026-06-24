from django.db import migrations


def resync_builtin_formats(apps, schema_editor):
    # Upgrade the global POSH built-in format in place so its column_mapping
    # includes "Was Processed In Person" -> processed_in_person, letting door
    # sales (no contact info) import instead of failing validation.
    from tickets.builtin_formats import sync_builtin_formats
    sync_builtin_formats()


def noop_reverse(apps, schema_editor):
    # The mapping lives in code; re-running the sync is the only "undo" and
    # there is nothing data-specific to reverse here.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0157_posh_revenue_subtotal'),
    ]

    operations = [
        migrations.RunPython(resync_builtin_formats, noop_reverse),
    ]
