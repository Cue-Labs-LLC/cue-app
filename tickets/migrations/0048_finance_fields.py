import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('tickets', '0047_userprofile_stripe_fields'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='organization',
            name='stripe_account_id',
            field=models.CharField(
                blank=True,
                max_length=255,
                help_text='Stripe Connect Express account ID (acct_xxx).',
            ),
        ),
        migrations.AddField(
            model_name='organization',
            name='stripe_onboarding_complete',
            field=models.BooleanField(
                default=False,
                help_text='True after Stripe confirms details_submitted and charges_enabled.',
            ),
        ),
        migrations.CreateModel(
            name='Payout',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=10)),
                ('stripe_transfer_id', models.CharField(
                    blank=True, max_length=255, null=True, unique=True,
                    help_text='Stripe Transfer ID (tr_xxx) — set after successful call.',
                )),
                ('status', models.CharField(
                    choices=[('pending', 'Pending'), ('completed', 'Completed'), ('failed', 'Failed')],
                    db_index=True, default='pending', max_length=20,
                )),
                ('notes', models.CharField(blank=True, max_length=500)),
                ('organization', models.ForeignKey(
                    db_index=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name='payouts', to='tickets.organization',
                )),
                ('initiated_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='initiated_payouts', to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='payout',
            index=models.Index(fields=['organization', 'status'], name='tickets_payout_org_stat_idx'),
        ),
        migrations.AddIndex(
            model_name='payout',
            index=models.Index(fields=['organization', '-created_at'], name='tickets_payout_org_date_idx'),
        ),
    ]
