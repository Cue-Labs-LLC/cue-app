from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0168_organization_segment_bands_organization_segment_mode'),
    ]

    operations = [
        migrations.CreateModel(
            name='Market',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(db_index=True, max_length=120)),
                ('geography_level', models.CharField(choices=[('city', 'City'), ('state', 'State'), ('country', 'Country')], db_index=True, max_length=20)),
                ('geography_value', models.CharField(db_index=True, max_length=120)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='markets', to='tickets.organization')),
            ],
            options={
                'ordering': ['geography_level', 'name'],
                'unique_together': {('organization', 'name'), ('organization', 'geography_level', 'geography_value')},
            },
        ),
        migrations.AddField(
            model_name='event',
            name='market',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='events', to='tickets.market'),
        ),
        migrations.AddIndex(
            model_name='market',
            index=models.Index(fields=['organization', 'geography_level', 'geography_value'], name='tickets_mar_organiz_6f9351_idx'),
        ),
        migrations.AddIndex(
            model_name='event',
            index=models.Index(fields=['organization', 'market', '-start_date'], name='tickets_eve_organiz_7d8609_idx'),
        ),
    ]
