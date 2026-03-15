from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0070_waitlist'),
    ]

    operations = [
        migrations.AddField(
            model_name='organization',
            name='waitlist_feature_enabled',
            field=models.BooleanField(default=False, help_text='Enable the waitlist feature for this organization.'),
        ),
    ]
