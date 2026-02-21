import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def start_phone_verification(phone: str) -> bool:
    """Start a Twilio Verify verification for the given phone number via SMS.

    Returns True on success, False on failure (e.g. invalid number, Twilio error).
    """
    try:
        from twilio.rest import Client
        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        client.verify.v2.services(settings.TWILIO_VERIFY_SERVICE_SID) \
            .verifications.create(to=phone, channel='sms')
        return True
    except Exception as exc:
        logger.error("Verify start failed for %s: %s", phone, exc)
        return False


def check_phone_verification(phone: str, code: str) -> bool:
    """Check a Twilio Verify code. Returns True if the code is approved."""
    try:
        from twilio.rest import Client
        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        result = client.verify.v2.services(settings.TWILIO_VERIFY_SERVICE_SID) \
            .verification_checks.create(to=phone, code=code)
        return result.status == 'approved'
    except Exception as exc:
        logger.error("Verify check failed for %s: %s", phone, exc)
        return False
