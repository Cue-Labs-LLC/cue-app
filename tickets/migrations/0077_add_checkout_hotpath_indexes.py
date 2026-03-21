from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0076_make_event_public_id_unique'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='ticketorder',
            index=models.Index(fields=['customer', 'refunded_at'], name='tickets_tic_custome_refund_idx'),
        ),
        migrations.AddIndex(
            model_name='waitlistentry',
            index=models.Index(fields=['ticket_type', 'email', 'expired', 'purchased_at'], name='tickets_wai_tt_email_exp_pur_idx'),
        ),
    ]
