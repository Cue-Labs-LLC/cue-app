# Data migration: create "Event Type" custom field and options "Day Party", "Night Party"

from django.db import migrations


def create_event_type_field(apps, schema_editor):
    CustomField = apps.get_model('tickets', 'CustomField')
    CustomFieldOption = apps.get_model('tickets', 'CustomFieldOption')
    event_type = CustomField.objects.create(
        name='Event Type',
        field_type='dropdown',
        order=0,
    )
    CustomFieldOption.objects.create(custom_field=event_type, label='Day Party', order=0)
    CustomFieldOption.objects.create(custom_field=event_type, label='Night Party', order=1)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0014_add_custom_field_models'),
    ]

    operations = [
        migrations.RunPython(create_event_type_field, noop),
    ]
