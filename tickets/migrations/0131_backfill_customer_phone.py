from django.db import migrations


def backfill_customer_phone(apps, schema_editor):
    Customer = apps.get_model('tickets', 'Customer')
    TicketOrder = apps.get_model('tickets', 'TicketOrder')
    User = apps.get_model('auth', 'User')
    UserProfile = apps.get_model('tickets', 'UserProfile')

    direct_customer_ids = (
        TicketOrder.objects
        .filter(uploaded_file__isnull=True)
        .values_list('customer_id', flat=True)
        .distinct()
    )
    customers = Customer.objects.filter(id__in=direct_customer_ids, phone='')

    emails = list(customers.values_list('email', flat=True))
    if not emails:
        return

    user_email_id = User.objects.filter(email__in=emails).values_list('email', 'id')
    lower_user_ids = {email.lower(): uid for email, uid in user_email_id}

    phone_by_uid = dict(
        UserProfile.objects
        .filter(user_id__in=lower_user_ids.values())
        .exclude(phone_number__isnull=True)
        .exclude(phone_number='')
        .values_list('user_id', 'phone_number')
    )

    to_update = []
    for c in customers.iterator():
        uid = lower_user_ids.get(c.email.lower())
        if uid is None:
            continue
        phone = phone_by_uid.get(uid)
        if phone:
            c.phone = phone
            to_update.append(c)

    Customer.objects.bulk_update(to_update, ['phone'], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0130_merge_20260522_1009'),
    ]

    operations = [
        migrations.RunPython(backfill_customer_phone, migrations.RunPython.noop),
    ]
