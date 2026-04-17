from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0100_eventdailypageview'),
    ]

    operations = [
        migrations.AddField(
            model_name='event',
            name='show_social_proof',
            field=models.BooleanField(
                default=True,
                help_text='Display attendee avatars and count on the public buy page.',
            ),
        ),
        migrations.AddField(
            model_name='event',
            name='show_attendee_count',
            field=models.BooleanField(
                default=True,
                help_text="Show the exact number of others (e.g. '+ 5 others'). When off, shows '+ others'.",
            ),
        ),
    ]
