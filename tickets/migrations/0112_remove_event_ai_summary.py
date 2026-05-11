from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0111_airecommendation'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='event',
            name='ai_summary',
        ),
        migrations.RemoveField(
            model_name='event',
            name='ai_summary_generated_at',
        ),
    ]
