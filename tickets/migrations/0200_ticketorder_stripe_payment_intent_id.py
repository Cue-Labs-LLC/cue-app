from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0199_backfill_launched_plan_step_bodies'),
    ]

    operations = [
        migrations.AddField(
            model_name='ticketorder',
            name='stripe_payment_intent_id',
            field=models.CharField(blank=True, db_index=True, help_text='Stripe PaymentIntent ID for in-person/Tap-to-Pay sales. Used to attach Stripe-sent receipts.', max_length=255),
        ),
    ]
