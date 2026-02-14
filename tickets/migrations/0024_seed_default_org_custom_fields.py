# Data migration: seed custom fields for default organization

from django.db import migrations


def seed_default_org_custom_fields(apps, schema_editor):
    Organization = apps.get_model('tickets', 'Organization')
    CustomField = apps.get_model('tickets', 'CustomField')
    CustomFieldOption = apps.get_model('tickets', 'CustomFieldOption')

    default_org, _ = Organization.objects.get_or_create(
        slug='default',
        defaults={'name': 'Default Organization'},
    )

    fields_with_options = [
        ('Venue Type', ['Standard', 'Buildout (The Beehive, Rolling Greens, etc...)']),
        ('Age requirement', ['21+']),
        ('Dress Code', ['No dress code, but come fly.']),
        ('Re-entry allowed', ['Yes', 'No']),
        ('Food Available', ['Yes', 'No']),
        ('Parking Info', ['Limited Parking available, rideshare', 'Street parking available']),
    ]

    for order, (field_name, option_labels) in enumerate(fields_with_options):
        if CustomField.objects.filter(organization=default_org, name=field_name).exists():
            continue
        cf = CustomField.objects.create(
            organization=default_org,
            name=field_name,
            field_type='dropdown',
            order=order,
        )
        for i, label in enumerate(option_labels):
            CustomFieldOption.objects.create(custom_field=cf, label=label, order=i)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0023_event_unique_together_organization'),
    ]

    operations = [
        migrations.RunPython(seed_default_org_custom_fields, noop),
    ]
