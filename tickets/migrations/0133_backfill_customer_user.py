from django.db import migrations


def backfill_customer_user(apps, schema_editor):
    Customer = apps.get_model('tickets', 'Customer')
    User = apps.get_model('auth', 'User')

    customers = Customer.objects.filter(user__isnull=True)
    emails = list(customers.values_list('email', flat=True))
    if not emails:
        return

    user_email_id = User.objects.filter(email__in=emails).values_list('email', 'id')
    user_id_by_email = {}
    for email, uid in user_email_id:
        user_id_by_email.setdefault(email.lower(), uid)

    to_update = []
    for c in customers.iterator():
        uid = user_id_by_email.get(c.email.lower())
        if uid is not None:
            c.user_id = uid
            to_update.append(c)

    Customer.objects.bulk_update(to_update, ['user'], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0132_customer_user'),
    ]

    operations = [
        migrations.RunPython(backfill_customer_user, migrations.RunPython.noop),
    ]
