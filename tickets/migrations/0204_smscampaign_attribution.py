from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0203_alter_aitokenusage_feature'),
    ]

    operations = [
        migrations.AddField(
            model_name='smscampaign',
            name='attributed_orders',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='smscampaign',
            name='attributed_revenue',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
    ]
