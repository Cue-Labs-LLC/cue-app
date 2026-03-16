import secrets
import string
from django.db import migrations


def populate_public_ids(apps, schema_editor):
    Event = apps.get_model('tickets', 'Event')
    alphabet = string.ascii_letters + string.digits
    for event in Event.objects.filter(public_id__isnull=True):
        while True:
            candidate = ''.join(secrets.choice(alphabet) for _ in range(10))
            if not Event.objects.filter(public_id=candidate).exists():
                event.public_id = candidate
                event.save(update_fields=['public_id'])
                break


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0074_add_event_public_id_nullable'),
    ]

    operations = [
        migrations.RunPython(populate_public_ids, migrations.RunPython.noop),
    ]
