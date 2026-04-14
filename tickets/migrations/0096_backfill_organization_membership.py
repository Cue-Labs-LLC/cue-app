from django.db import migrations


def backfill_memberships(apps, schema_editor):
    """Create OrganizationMembership rows from existing UserProfile.organization data."""
    UserProfile = apps.get_model('tickets', 'UserProfile')
    OrganizationMembership = apps.get_model('tickets', 'OrganizationMembership')
    for profile in UserProfile.objects.select_related('organization', 'user').filter(
        organization__isnull=False
    ):
        OrganizationMembership.objects.get_or_create(
            user=profile.user,
            organization=profile.organization,
            defaults={'org_role': profile.org_role},
        )


def reverse_backfill(apps, schema_editor):
    # Reverse is a no-op: source data in UserProfile is untouched.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0095_add_organization_membership'),
    ]

    operations = [
        migrations.RunPython(backfill_memberships, reverse_backfill),
    ]
