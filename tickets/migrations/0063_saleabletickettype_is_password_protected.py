from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0062_saleabletickettype_password'),
    ]

    operations = [
        migrations.AddField(
            model_name='saleabletickettype',
            name='is_password_protected',
            field=models.BooleanField(
                default=False,
                help_text='If checked, this ticket type is hidden until a customer enters the password below.',
            ),
        ),
    ]
