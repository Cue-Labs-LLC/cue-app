from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0061_event_flyer_org_path'),
    ]

    operations = [
        migrations.AddField(
            model_name='saleabletickettype',
            name='password',
            field=models.CharField(
                blank=True,
                help_text='If set, this ticket type is hidden on the public page until a customer enters this password.',
                max_length=100,
            ),
        ),
    ]
