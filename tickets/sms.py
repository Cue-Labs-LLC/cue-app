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

# First http(s) URL in a string. v1 link-click tracking rewrites this one URL;
# bare domains without a scheme are intentionally not matched.
_URL_RE = re.compile(r'https?://\S+')


def extract_first_url(text: str) -> str:
    """Return the first http(s):// URL in text (trailing punctuation stripped), or ''."""
    if not text:
        return ''
    match = _URL_RE.search(text)
    if not match:
        return ''
    return match.group(0).rstrip('.,!?;:)\'"')


# Twilio error codes meaning the recipient is opted out (on the carrier/Twilio STOP
# list). Treated as a suppression signal: mirror into PhoneSuppression so the number
# is never re-attempted. The inbound OptOutType webhook can miss opt-outs (legacy STOPs
# from before suppression tracking existed, or a dropped/failed callback), and Twilio
# then blocks each retry — dinging the account's Compliance score. Consumed by
# send_sms_chunk_task and twilio_sms_status_webhook. Compare as strings.
TWILIO_OPT_OUT_ERROR_CODES = frozenset({'21610'})


def sms_country_allowed(phone: str) -> bool:
    """True if `phone` (E.164) is in a country enabled for marketing SMS.

    Guards against Twilio Geo-Permission blocks (Error 21408) and non-billable sends to
    countries we don't serve. Configured via SMS_ALLOWED_COUNTRY_PREFIXES (E.164
    calling-code prefixes, default US/CA '+1'); an empty setting allows all countries.
    Expects an already-normalized E.164 number (see normalize_phone).
    """
    prefixes = getattr(settings, 'SMS_ALLOWED_COUNTRY_PREFIXES', ('+1',))
    if not prefixes:
        return True
    return any((phone or '').startswith(p) for p in prefixes)


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


# Explicit opt-out phrasing ("Reply STOP", "Text STOP anytime", …). Only this
# suppresses the auto-appended footer — a casual "stop by the bar" must not strip
# the compliance disclosure. Mirrored in campaign_form.html's live meter.
_OPT_OUT_RE = re.compile(r'\b(?:reply|text|send)\s+["“]?STOP\b', re.IGNORECASE)


def apply_stop_footer(body: str, *, include: bool = True):
    """Return ``(final_body, disclosure_present)``.

    If the body already carries explicit opt-out phrasing it is left untouched and
    ``disclosure_present`` is True (the copy itself discloses — never double-append).
    Otherwise the footer is appended only when ``include`` is True;
    ``disclosure_present`` mirrors whether the outgoing text carries a disclosure.

    Conditional inclusion drives the disclosure cadence: include the footer on the
    first message to a phone and periodically after (see SMS_FOOTER_DISCLOSURE_DAYS),
    not on every message. Twilio's Messaging Service enforces STOP regardless of this
    text, so omitting it for an already-disclosed recipient does not break opt-out."""
    if _OPT_OUT_RE.search(body or ''):
        return body, True
    if include:
        return f"{body}\n\nReply STOP to opt out", True
    return body, False


def with_stop_footer(body: str) -> str:
    """Always-append wrapper around :func:`apply_stop_footer` (worst-case display).

    Used by the cost estimators and the server-rendered composer meter, which show
    the maximum a message could cost. The send path uses ``apply_stop_footer``
    directly with a per-recipient ``include`` decision."""
    return apply_stop_footer(body, include=True)[0]


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

    Returns (ok, message_sid, error_code). error_code is the Twilio error code (as a
    string) when the send is rejected — notably '21610' for an opted-out recipient,
    which the caller mirrors into PhoneSuppression. None when there's no Twilio code.
    In E2E test mode no Twilio call is made.
    """
    if getattr(settings, 'E2E_TEST_MODE', False):
        logger.info("E2E test mode: pretending to send SMS to %s", to)
        return True, 'E2E_FAKE_SID', None
    messaging_service_sid = getattr(settings, 'TWILIO_MESSAGING_SERVICE_SID', '')
    from_number = getattr(settings, 'TWILIO_SMS_FROM', '')
    if not messaging_service_sid and not from_number:
        logger.error(
            "Cannot send SMS: set TWILIO_MESSAGING_SERVICE_SID (preferred) or TWILIO_SMS_FROM."
        )
        return False, None, None
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
        return True, message.sid, None
    except Exception as exc:
        # An opted-out recipient (21610) surfaces synchronously here, not via the status
        # callback, so return the code for the caller to act on. TwilioRestException
        # exposes .code (int); anything else has no code.
        code = getattr(exc, 'code', None)
        logger.error("SMS send failed for %s: %s", to, exc)
        return False, None, str(code) if code is not None else None


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


# ---------------------------------------------------------------------------
# Thin OTP session helpers — the shared mechanic behind the public /join/ and
# /subscribe/ flows. Attempt/expiry limits live inside Twilio Verify (NOT the
# PhoneOTP model), so these only wrap start/check + stash the pending phone in
# the session. Each view keeps its own terminal write (User vs Customer+consent).
# ---------------------------------------------------------------------------

def otp_start(request, phone: str, *, purpose: str) -> bool:
    """Send a Verify code and stash the pending phone in the session.

    Returns True if the code was sent. `purpose` namespaces the session key so
    concurrent flows (e.g. signup vs subscribe) don't collide.
    """
    if start_phone_verification(phone):
        request.session[f'otp_pending_phone:{purpose}'] = phone
        return True
    return False


def otp_check(request, code: str, *, purpose: str):
    """Verify `code` against the phone stashed by :func:`otp_start`.

    Returns ``(ok, phone)``. `phone` is '' when there is no pending verification
    for this purpose (session expired / step reached out of order).
    """
    phone = request.session.get(f'otp_pending_phone:{purpose}', '')
    if not phone:
        return False, ''
    return check_phone_verification(phone, code), phone


def otp_clear(request, *, purpose: str) -> None:
    """Drop the pending-phone session key for a purpose (call after success)."""
    request.session.pop(f'otp_pending_phone:{purpose}', None)


def resolve_sms_sender_number() -> str:
    """Best-effort resolution of the number a user should text 'START' to.

    Marketing sends go through a Twilio Messaging Service (no single fixed
    number), so this prefers the explicit TWILIO_SMS_FROM; otherwise it looks up
    the first phone number on the Messaging Service (cached for an hour). Returns
    '' when nothing resolves — callers then show a generic "reply START to any
    message from us" instead of a wrong number.
    """
    from_number = getattr(settings, 'TWILIO_SMS_FROM', '')
    if from_number:
        return from_number
    if getattr(settings, 'E2E_TEST_MODE', False):
        return ''
    msid = getattr(settings, 'TWILIO_MESSAGING_SERVICE_SID', '')
    if not msid:
        return ''
    from django.core.cache import cache
    cached = cache.get('sms_sender_number')
    if cached is not None:
        return cached
    resolved = ''
    try:
        from twilio.rest import Client
        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        numbers = client.messaging.v1.services(msid).phone_numbers.list(limit=5)
        if numbers:
            resolved = numbers[0].phone_number or ''
    except Exception as exc:
        logger.error("Could not resolve SMS sender number: %s", exc)
    cache.set('sms_sender_number', resolved, 3600)
    return resolved


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
