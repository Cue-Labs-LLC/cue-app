from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0202_organization_brand_voice_guidelines'),
    ]

    operations = [
        migrations.AlterField(
            model_name='aitokenusage',
            name='feature',
            field=models.CharField(choices=[('chat_agent', 'Chat agent'), ('meta_campaign_match', 'Meta campaign match'), ('mailchimp_campaign_match', 'Mailchimp campaign match'), ('slicktext_campaign_match', 'SlickText campaign match'), ('marketing_narrative', 'Marketing narrative'), ('typeform_event_match', 'Typeform event match'), ('event_summary', 'Event summary'), ('sms_plan', 'SMS campaign plan'), ('brand_voice_example', 'Brand voice example')], db_index=True, max_length=50),
        ),
    ]
