# Data migration: set user on existing CustomField rows (first superuser or first user)

from django.db import migrations


def backfill_customfield_user(apps, schema_editor):
    CustomField = apps.get_model('tickets', 'CustomField')
    User = apps.get_model('auth', 'User')
    if not CustomField.objects.filter(user__isnull=True).exists():
        return
    fallback = User.objects.filter(is_superuser=True).first()
    if fallback is None:
        fallback = User.objects.first()
    if fallback is None:
        # Fresh DB with seeded CustomField rows but no users yet — create
        # a placeholder so the subsequent NOT NULL migration succeeds.
        # Migration 0021 removes this field entirely.
        fallback = User.objects.create(
            username='_migration_placeholder',
            is_active=False,
        )
    CustomField.objects.filter(user__isnull=True).update(user=fallback)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0016_add_customfield_user'),
    ]

    operations = [
        migrations.RunPython(backfill_customfield_user, noop),
    ]
