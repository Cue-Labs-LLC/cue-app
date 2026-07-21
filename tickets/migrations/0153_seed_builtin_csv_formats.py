from django.db import migrations


def seed_builtin_formats(apps, schema_editor):
    # Use the live helper/registry so existing and fresh databases get the same
    # built-in formats. update_or_create keeps this idempotent and upgrade-safe.
    from tickets.builtin_formats import sync_builtin_formats
    sync_builtin_formats()


def remove_builtin_formats(apps, schema_editor):
    CSVFormat = apps.get_model('tickets', 'CSVFormat')
    from tickets.builtin_formats import BUILTIN_CSV_FORMATS
    names = [spec['name'] for spec in BUILTIN_CSV_FORMATS]
    CSVFormat.objects.filter(
        name__in=names, organization__isnull=True, is_system=True
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0152_csvformat_is_system_ticket_scanned_at_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_builtin_formats, remove_builtin_formats),
    ]
