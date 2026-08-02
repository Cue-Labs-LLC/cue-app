from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0201_merge_20260724_0954'),
    ]

    operations = [
        migrations.AddField(
            model_name='organization',
            name='brand_voice_guidelines',
            field=models.TextField(blank=True, default='', help_text='How AI-written marketing messages should sound (tone, formality, phrases to use or avoid). Takes precedence over the voice auto-detected from your past messages.'),
        ),
    ]
