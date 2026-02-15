# Data migration: seed "Accepting Vendors" custom field for default organization

from django.db import migrations


def seed_accepting_vendors_custom_field(apps, schema_editor):
    Organization = apps.get_model('tickets', 'Organization')
    CustomField = apps.get_model('tickets', 'CustomField')
    CustomFieldOption = apps.get_model('tickets', 'CustomFieldOption')

    default_org, _ = Organization.objects.get_or_create(
        slug='default',
        defaults={'name': 'Default Organization'},
    )

    field_name = 'Accepting Vendors'
    if CustomField.objects.filter(organization=default_org, name=field_name).exists():
        return

    cf = CustomField.objects.create(
        organization=default_org,
        name=field_name,
        field_type='dropdown',
        order=100,
    )
    for i, label in enumerate(['Yes', 'No']):
        CustomFieldOption.objects.create(custom_field=cf, label=label, order=i)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0029_add_organization_rfm_recalc_in_progress'),
    ]

    operations = [
        migrations.RunPython(seed_accepting_vendors_custom_field, noop),
    ]
