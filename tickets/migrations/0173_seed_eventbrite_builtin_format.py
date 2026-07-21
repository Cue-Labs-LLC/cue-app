"""Sync built-in CSV formats so the new Eventbrite format is seeded.

sync_builtin_formats() is idempotent (update_or_create keyed on name), so this
re-runs the same helper 0153 used — existing POSH stays, Eventbrite is added.
"""

from django.db import migrations


def seed_builtin_formats(apps, schema_editor):
    from tickets.builtin_formats import sync_builtin_formats
    sync_builtin_formats()


def remove_eventbrite(apps, schema_editor):
    CSVFormat = apps.get_model('tickets', 'CSVFormat')
    CSVFormat.objects.filter(
        name='Eventbrite', organization__isnull=True, is_system=True
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0172_backfill_external_events_enabled'),
    ]

    operations = [
        migrations.RunPython(seed_builtin_formats, remove_eventbrite),
    ]
