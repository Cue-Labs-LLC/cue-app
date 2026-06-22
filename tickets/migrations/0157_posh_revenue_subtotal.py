from django.db import migrations


def resync_builtin_formats(apps, schema_editor):
    # Upgrade the global POSH built-in format in place so its revenue mapping
    # (total_amount) points at "Order Subtotal" instead of the fee-inclusive
    # "Order Total". update_or_create keyed on name keeps this idempotent.
    from tickets.builtin_formats import sync_builtin_formats
    sync_builtin_formats()


def noop_reverse(apps, schema_editor):
    # Reverting just re-runs the sync against whatever the registry holds; the
    # mapping itself lives in code, so there is nothing data-specific to undo.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0156_survey_builder'),
    ]

    operations = [
        migrations.RunPython(resync_builtin_formats, noop_reverse),
    ]
