import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)
E2E_OTP_CODE = '000000'

# GSM-7 default alphabet. Anything outside it forces a message to UCS-2 encoding,
# which shrinks the per-segment character budget from 160 to 70.
_GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ ÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_EXTENDED = "^{}\\[~]|€"
_GSM7_CHARS = set(_GSM7_BASIC) | set(_GSM7_EXTENDED)


def normalize_phone(raw: str) -> str:
    """Normalize a phone number string to E.164 (+1XXXXXXXXXX for US numbers).

    Canonical implementation — `tickets.forms._normalize_phone` delegates here so
    suppression and dedupe keys match what auth/checkout store.
    """
    if not raw:
        return ''
    phone = raw.strip()
    if not phone.startswith('+'):
        digits = re.sub(r'\D', '', phone)
        if len(digits) == 10:
            phone = '+1' + digits
        elif len(digits) == 11 and digits[0] == '1':
            phone = '+' + digits
        else:
            phone = '+' + digits  # let downstream validation catch invalid formats
    return phone


def sms_segment_info(body: str):
    """Return (encoding, segment_count) for an SMS body.

    Accounts for GSM-7 vs UCS-2 so the compose UI can show a truthful segment
    count. Extended GSM-7 chars cost 2 code units each.
    """
    body = body or ''
    is_unicode = any(ch not in _GSM7_CHARS for ch in body)
    if is_unicode:
        length = len(body)
        single, multi = 70, 67
        encoding = 'UCS-2'
    else:
        length = sum(2 if ch in _GSM7_EXTENDED else 1 for ch in body)
        single, multi = 160, 153
        encoding = 'GSM-7'
    if length == 0:
        segments = 1
    elif length <= single:
        segments = 1
    else:
        segments = -(-length // multi)  # ceil division
    return encoding, segments


def send_sms(to: str, body: str, status_callback: str | None = None):
    """Send a marketing SMS via the configured Twilio Messaging Service.

    Returns (ok, message_sid). In E2E test mode no Twilio call is made.
    """
    if getattr(settings, 'E2E_TEST_MODE', False):
        logger.info("E2E test mode: pretending to send SMS to %s", to)
        return True, 'E2E_FAKE_SID'
    messaging_service_sid = getattr(settings, 'TWILIO_MESSAGING_SERVICE_SID', '')
    from_number = getattr(settings, 'TWILIO_SMS_FROM', '')
    if not messaging_service_sid and not from_number:
        logger.error(
            "Cannot send SMS: set TWILIO_MESSAGING_SERVICE_SID (preferred) or TWILIO_SMS_FROM."
        )
        return False, None
    try:
        from twilio.rest import Client
        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        kwargs = {'to': to, 'body': body}
        # Prefer the Messaging Service (Advanced Opt-Out); else the verified number.
        if messaging_service_sid:
            kwargs['messaging_service_sid'] = messaging_service_sid
        else:
            kwargs['from_'] = from_number
        if status_callback:
            kwargs['status_callback'] = status_callback
        message = client.messages.create(**kwargs)
        return True, message.sid
    except Exception as exc:
        logger.error("SMS send failed for %s: %s", to, exc)
        return False, None


def validate_twilio_request(request) -> bool:
    """Validate an inbound Twilio webhook signature.

    Bypassed in E2E test mode or when TWILIO_VALIDATE_WEBHOOKS is False (dev).
    """
    if getattr(settings, 'E2E_TEST_MODE', False):
        return True
    if not getattr(settings, 'TWILIO_VALIDATE_WEBHOOKS', True):
        return True
    try:
        from twilio.request_validator import RequestValidator
        validator = RequestValidator(settings.TWILIO_AUTH_TOKEN)
        signature = request.headers.get('X-Twilio-Signature', '')
        # Build the absolute URL Twilio signed against (respects the SSL proxy).
        url = request.build_absolute_uri()
        return validator.validate(url, request.POST.dict(), signature)
    except Exception as exc:
        logger.error("Twilio signature validation error: %s", exc)
        return False


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
