import logging

from django.conf import settings

logger = logging.getLogger(__name__)
E2E_OTP_CODE = '000000'


def start_phone_verification(phone: str) -> bool:
    """Start a Twilio Verify verification for the given phone number via SMS.

    Returns True on success, False on failure (e.g. invalid number, Twilio error).
    """
    if getattr(settings, 'E2E_TEST_MODE', False):
        logger.info("E2E test mode: accepting phone verification start for %s", phone)
        return True
    try:
        from twilio.rest import Client
        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        svc = client.verify.v2.services(settings.TWILIO_VERIFY_SERVICE_SID)
        try:
            svc.verifications(phone).update(status='canceled')
        except Exception:
            pass
        svc.verifications.create(to=phone, channel='sms')
        return True
    except Exception as exc:
        logger.error("Verify start failed for %s: %s", phone, exc)
        return False


def check_phone_verification(phone: str, code: str) -> bool:
    """Check a Twilio Verify code. Returns True if the code is approved."""
    if getattr(settings, 'E2E_TEST_MODE', False):
        return code == E2E_OTP_CODE
    try:
        from twilio.rest import Client
        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        result = client.verify.v2.services(settings.TWILIO_VERIFY_SERVICE_SID) \
            .verification_checks.create(to=phone, code=code)
        return result.status == 'approved'
    except Exception as exc:
        logger.error("Verify check failed for %s: %s", phone, exc)
        return False


def start_email_verification(email: str) -> bool:
    """Start a Twilio Verify verification for the given email address.

    Returns True on success, False on failure.
    """
    if getattr(settings, 'E2E_TEST_MODE', False):
        logger.info("E2E test mode: accepting email verification start for %s", email)
        return True
    try:
        from twilio.rest import Client
        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        svc = client.verify.v2.services(settings.TWILIO_VERIFY_SERVICE_SID)
        try:
            svc.verifications(email).update(status='canceled')
        except Exception:
            pass
        svc.verifications.create(to=email, channel='email')
        return True
    except Exception as exc:
        logger.error("Email verify start failed for %s: %s", email, exc)
        return False


def check_email_verification(email: str, code: str) -> bool:
    """Check a Twilio Verify email code. Returns True if the code is approved."""
    if getattr(settings, 'E2E_TEST_MODE', False):
        return code == E2E_OTP_CODE
    try:
        from twilio.rest import Client
        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        result = client.verify.v2.services(settings.TWILIO_VERIFY_SERVICE_SID) \
            .verification_checks.create(to=email, code=code)
        return result.status == 'approved'
    except Exception as exc:
        logger.error("Email verify check failed for %s: %s", email, exc)
        return False
