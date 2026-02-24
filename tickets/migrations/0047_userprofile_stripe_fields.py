from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('tickets', '0046_userprofile_gender_marketing_opt_in')]
    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='stripe_customer_id',
            field=models.CharField(
                blank=True, null=True, max_length=255, db_index=True,
                help_text='Stripe Customer ID (cus_xxx).',
            ),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='stripe_pm_id',
            field=models.CharField(
                blank=True, null=True, max_length=255,
                help_text='Saved Stripe PaymentMethod ID (pm_xxx).',
            ),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='stripe_pm_brand',
            field=models.CharField(
                blank=True, max_length=50,
                help_text="Card brand, e.g. 'visa'.",
            ),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='stripe_pm_last4',
            field=models.CharField(
                blank=True, max_length=4,
                help_text='Last 4 digits of saved card.',
            ),
        ),
    ]
