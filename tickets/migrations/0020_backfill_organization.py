# Data migration: create default organization, UserProfiles for all users, backfill org on all entities

from django.db import migrations


def backfill_organization(apps, schema_editor):
    Organization = apps.get_model('tickets', 'Organization')
    UserProfile = apps.get_model('tickets', 'UserProfile')
    User = apps.get_model('auth', 'User')
    Venue = apps.get_model('tickets', 'Venue')
    Event = apps.get_model('tickets', 'Event')
    UploadedFile = apps.get_model('tickets', 'UploadedFile')
    Customer = apps.get_model('tickets', 'Customer')
    CSVFormat = apps.get_model('tickets', 'CSVFormat')
    CustomField = apps.get_model('tickets', 'CustomField')

    # Create default organization
    default_org, _ = Organization.objects.get_or_create(
        slug='default',
        defaults={'name': 'Default Organization'},
    )

    # Create UserProfile for each user, assigned to default org
    for user in User.objects.all():
        UserProfile.objects.get_or_create(
            user=user,
            defaults={'organization': default_org},
        )

    # Backfill organization on all entities
    Venue.objects.filter(organization__isnull=True).update(organization=default_org)
    Event.objects.filter(organization__isnull=True).update(organization=default_org)
    UploadedFile.objects.filter(organization__isnull=True).update(organization=default_org)
    Customer.objects.filter(organization__isnull=True).update(organization=default_org)
    CSVFormat.objects.filter(organization__isnull=True).update(organization=default_org)

    # CustomField: set org from the field's user's profile (or default org if no profile)
    for cf in CustomField.objects.filter(organization__isnull=True):
        try:
            profile = UserProfile.objects.get(user=cf.user_id)
            cf.organization = profile.organization
            cf.save()
        except UserProfile.DoesNotExist:
            cf.organization = default_org
            cf.save()


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0019_add_organization_and_userprofile'),
    ]

    operations = [
        migrations.RunPython(backfill_organization, noop),
    ]
