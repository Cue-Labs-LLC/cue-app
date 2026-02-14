from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0008_migrate_event_date_data'),
    ]

    operations = [
        # 1. Remove old unique_together constraint
        migrations.AlterUniqueTogether(
            name='event',
            unique_together=set(),
        ),
        # 2. Remove old index
        migrations.RemoveIndex(
            model_name='event',
            name='tickets_eve_name_ac155f_idx',
        ),
        # 3. Remove old field
        migrations.RemoveField(
            model_name='event',
            name='event_date',
        ),
        # 4. Make start_date non-nullable
        migrations.AlterField(
            model_name='event',
            name='start_date',
            field=models.DateField(db_index=True),
        ),
        # 5. Add new unique_together
        migrations.AlterUniqueTogether(
            name='event',
            unique_together={('name', 'start_date')},
        ),
        # 6. Add new index
        migrations.AddIndex(
            model_name='event',
            index=models.Index(fields=['name', 'start_date'], name='tickets_eve_name_fb9c20_idx'),
        ),
        # 7. Update ordering
        migrations.AlterModelOptions(
            name='event',
            options={'ordering': ['-start_date', '-start_time', 'name']},
        ),
    ]
