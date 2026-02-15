# Data migration: set user on existing CustomField rows (first superuser or first user)

from django.db import migrations


def backfill_customfield_user(apps, schema_editor):
    CustomField = apps.get_model('tickets', 'CustomField')
    User = apps.get_model('auth', 'User')
    fallback = User.objects.filter(is_superuser=True).first()
    if fallback is None:
        fallback = User.objects.first()
    if fallback is None:
        return
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
