import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0069_saleabletickettype_unlocks_after'),
    ]

    operations = [
        migrations.AddField(
            model_name='saleabletickettype',
            name='waitlist_enabled',
            field=models.BooleanField(
                default=False,
                help_text='Allow buyers to join a waitlist when this ticket type is sold out.',
            ),
        ),
        migrations.AddField(
            model_name='saleabletickettype',
            name='quantity_held',
            field=models.PositiveIntegerField(
                default=0,
                help_text='Spots temporarily held for waitlist notifications. Counted as sold for availability purposes.',
            ),
        ),
        migrations.CreateModel(
            name='WaitlistEntry',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('email', models.EmailField(db_index=True, max_length=254)),
                ('name', models.CharField(blank=True, max_length=200)),
                ('position', models.PositiveIntegerField()),
                ('notified_at', models.DateTimeField(blank=True, null=True)),
                ('hold_expires_at', models.DateTimeField(blank=True, null=True)),
                ('purchased_at', models.DateTimeField(blank=True, null=True)),
                ('expired', models.BooleanField(default=False)),
                ('hold_token', models.UUIDField(db_index=True, default=uuid.uuid4, unique=True)),
                ('ticket_type', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='waitlist_entries',
                    to='tickets.saleabletickettype',
                )),
            ],
            options={
                'ordering': ['position'],
                'indexes': [
                    models.Index(fields=['ticket_type', 'position'], name='tickets_wai_ticket__idx'),
                    models.Index(fields=['hold_token'], name='tickets_wai_hold_to_idx'),
                ],
            },
        ),
    ]
