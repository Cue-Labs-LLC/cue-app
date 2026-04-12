from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0093_customer_sms_opt_in_customer_sms_opt_in_date_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='customer',
            name='avg_days_between_orders',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='customer',
            name='behavior_profile',
            field=models.CharField(blank=True, db_index=True, max_length=40),
        ),
        migrations.AddField(
            model_name='customer',
            name='behavior_profile_reason',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='customer',
            name='days_since_last_order',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='customer',
            name='days_to_second_order',
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
