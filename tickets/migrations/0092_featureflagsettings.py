from django.db import migrations, models


def create_feature_flag_settings(apps, schema_editor):
    FeatureFlagSettings = apps.get_model('tickets', 'FeatureFlagSettings')
    FeatureFlagSettings.objects.get_or_create(
        singleton_enforcer=True,
        defaults={
            'direct_ticketing_enabled': True,
            'browse_events_enabled': False,
            'smart_pricing_recommendations_enabled': False,
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0091_payout_in_transit_and_payout_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='FeatureFlagSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('singleton_enforcer', models.BooleanField(default=True, editable=False, unique=True)),
                ('direct_ticketing_enabled', models.BooleanField(default=True, help_text='Enable direct ticket selling flows for allowed users.')),
                ('browse_events_enabled', models.BooleanField(default=False, help_text='Expose the public Browse Events experience.')),
                ('smart_pricing_recommendations_enabled', models.BooleanField(default=False, help_text='Enable Smart Pricing Recommendations on direct-ticketing events.')),
            ],
            options={
                'verbose_name': 'Feature Flag Settings',
                'verbose_name_plural': 'Feature Flag Settings',
            },
        ),
        migrations.RunPython(create_feature_flag_settings, migrations.RunPython.noop),
    ]
