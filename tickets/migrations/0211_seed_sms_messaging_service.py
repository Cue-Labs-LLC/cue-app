from django.conf import settings
from django.db import migrations


def seed_from_env(apps, schema_editor):
    """Seed one active SMSMessagingService from the current env settings so the
    first deploy behaves identically to today. No-op if a row already exists or
    no Messaging Service SID is configured (e.g. dev)."""
    SMSMessagingService = apps.get_model('tickets', 'SMSMessagingService')
    if SMSMessagingService.objects.exists():
        return
    sid = getattr(settings, 'TWILIO_MESSAGING_SERVICE_SID', '') or ''
    sms_from = getattr(settings, 'TWILIO_SMS_FROM', '') or ''
    if not sid and not sms_from:
        return
    cap = getattr(settings, 'SMS_DAILY_SEGMENT_CAP', 2000)
    SMSMessagingService.objects.create(
        label='Default (from env)',
        messaging_service_sid=sid,
        sms_from=sms_from,
        daily_segment_cap=cap if cap and cap > 0 else 0,
        is_active=True,
    )


def unseed(apps, schema_editor):
    SMSMessagingService = apps.get_model('tickets', 'SMSMessagingService')
    SMSMessagingService.objects.filter(label='Default (from env)').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0210_smsmessagingservice'),
    ]

    operations = [
        migrations.RunPython(seed_from_env, unseed),
    ]
