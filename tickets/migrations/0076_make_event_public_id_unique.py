from django.db import migrations, models
import tickets.models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0075_populate_event_public_ids'),
    ]

    operations = [
        migrations.AlterField(
            model_name='event',
            name='public_id',
            field=models.CharField(
                db_index=True,
                default=tickets.models.generate_event_public_id,
                editable=False,
                help_text='Short public-facing ID used in shareable URLs (/e/<public_id>/).',
                max_length=10,
                unique=True,
            ),
        ),
    ]
