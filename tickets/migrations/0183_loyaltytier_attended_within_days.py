from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0182_merge_0181_loyaltytier_0181_smsconsentrecord'),
    ]

    operations = [
        migrations.RenameField(
            model_name='loyaltytier',
            old_name='max_days_since_last_attended',
            new_name='attended_within_days',
        ),
        migrations.AlterField(
            model_name='loyaltytier',
            name='attended_within_days',
            field=models.PositiveIntegerField(
                blank=True,
                null=True,
                help_text='Only count events attended within this many days (attendance window). '
                          'Leave blank to count attendance over all time.',
            ),
        ),
    ]
