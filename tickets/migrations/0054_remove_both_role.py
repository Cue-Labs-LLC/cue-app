from django.db import migrations, models


def convert_both_to_organizer(apps, schema_editor):
    UserProfile = apps.get_model('tickets', 'UserProfile')
    OrganizationInvitation = apps.get_model('tickets', 'OrganizationInvitation')
    UserProfile.objects.filter(role='both').update(role='organizer')
    OrganizationInvitation.objects.filter(role='both').update(role='organizer')


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0053_org_roles'),
    ]

    operations = [
        migrations.RunPython(convert_both_to_organizer, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='userprofile',
            name='role',
            field=models.CharField(
                choices=[('organizer', 'Organizer'), ('attendee', 'Attendee')],
                db_index=True,
                default='organizer',
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name='organizationinvitation',
            name='role',
            field=models.CharField(
                choices=[('organizer', 'Organizer'), ('attendee', 'Attendee')],
                default='organizer',
                max_length=20,
            ),
        ),
    ]
