"""
Replace the global unique constraint on Customer.email with a per-org
unique constraint on (organization, email).

Previously email was globally unique across all organizations, which broke
multi-tenant isolation: a customer who bought tickets from org A could not
be independently tracked by org B.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0102_alter_event_show_attendee_count'),
    ]

    operations = [
        # Drop the global unique constraint on email
        migrations.AlterField(
            model_name='customer',
            name='email',
            field=models.EmailField(db_index=True),
        ),
        # Add per-org unique constraint on (organization, email)
        migrations.AddConstraint(
            model_name='customer',
            constraint=models.UniqueConstraint(
                fields=['organization', 'email'],
                name='customer_org_email_unique',
            ),
        ),
    ]
