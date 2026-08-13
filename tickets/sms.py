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

# First link in a string that v1 link-click tracking should rewrite: an explicit
# http(s):// URL a user pasted, OR one of our own tracked short links (/t/, /track/
# or /c/<token>/) whose scheme was dropped to save SMS characters. The scheme-full
# alternative is listed first so pasted URLs are captured verbatim; bare domains
# without a recognized tracking path are still not matched.
_URL_RE = re.compile(
    r'https?://\S+'
    r'|(?:[\w-]+\.)+[\w-]+(?::\d+)?/(?:t|track|c)/[A-Za-z0-9_-]+/?'
    r'|localhost(?::\d+)?/(?:t|track|c)/[A-Za-z0-9_-]+/?'
)


def extract_first_url(text: str) -> str:
    """Return the first tracked/http(s) link in text (trailing punctuation stripped), or ''."""
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

# Twilio error codes meaning the destination is permanently undeliverable for EVERY
# sender: an unknown/unassigned handset (30005), a landline or unreachable carrier
# (30006), a malformed/invalid 'To' (21211), or a number that can't receive SMS
# (21614). Treated as a hard bounce → suppressed on first sight so no campaign wastes
# credits or daily-cap budget re-texting a dead number. Compare as strings.
TWILIO_HARD_BOUNCE_ERROR_CODES = frozenset({'30005', '30006', '21211', '21614'})

# Twilio error codes meaning the send failed but the number MIGHT still be reachable
# later — 30003 is a handset that was off / out of coverage. A single failure isn't
# proof the number is dead, so these accrue "strikes" across distinct campaigns and
# only suppress once SMS_BOUNCE_STRIKE_THRESHOLD blasts have failed the same number.
# Deliberately excludes 30007 (carrier spam-filtering — a sender-reputation problem,
# not the number's fault) and 21408 (geo-permission — a config/region concern handled
# by sms_country_allowed, not a dead number). Compare as strings.
TWILIO_TRANSIENT_FAIL_ERROR_CODES = frozenset({'30003'})


def _suppress_phone(phone, reason):
    """Idempotently record a GLOBAL PhoneSuppression for `phone`.

    Global (organization=None) because a dead/opted-out number is undeliverable for
    every org sharing the sender. Mirrors the get_or_create used by the opt-out paths.
    """
    from tickets.models import PhoneSuppression
    PhoneSuppression.objects.get_or_create(
        phone=phone, organization=None, defaults={'reason': reason},
    )


def handle_delivery_failure(phone, error_code):
    """Suppress `phone` when a failed/undelivered send warrants it.

    Single source of truth for failure → suppression, called from BOTH the send-path
    failure branch (send_sms_chunk_task) and the async status callback
    (twilio_sms_status_webhook) so the two agree. No-op unless the code is actionable:

      - opt-out (21610)      → suppress as TWILIO_STOP (Twilio's block can outrun the
                               inbound OptOutType webhook; learn from the block itself)
      - hard bounce          → suppress as BOUNCE immediately
      - transient fail       → suppress as BOUNCE only after enough per-campaign strikes

    Codes outside these sets (30007 carrier-filtering, 21408 geo, etc.) are intentionally
    left alone — they don't mean the number is dead.
    """
    from tickets.models import PhoneSuppression
    if not phone or not error_code:
        return
    error_code = str(error_code)
    if error_code in TWILIO_OPT_OUT_ERROR_CODES:
        _suppress_phone(phone, PhoneSuppression.Reason.TWILIO_STOP)
    elif error_code in TWILIO_HARD_BOUNCE_ERROR_CODES:
        _suppress_phone(phone, PhoneSuppression.Reason.BOUNCE)
    elif error_code in TWILIO_TRANSIENT_FAIL_ERROR_CODES:
        _maybe_suppress_after_strikes(phone)


def _maybe_suppress_after_strikes(phone):
    """Suppress `phone` as a BOUNCE once it has failed a transient code across at
    least SMS_BOUNCE_STRIKE_THRESHOLD *distinct* campaigns.

    Counts distinct campaigns (not rows) so "failed one blast" never counts as many
    strikes, and derives the tally from existing SMSMessageRecipient rows — no strike
    counter to store or migrate. Runs only on the small fraction of sends that fail.
    """
    from tickets.models import PhoneSuppression, SMSMessageRecipient
    threshold = getattr(settings, 'SMS_BOUNCE_STRIKE_THRESHOLD', 3)
    strikes = (
        SMSMessageRecipient.objects
        .filter(phone=phone, error_code__in=TWILIO_TRANSIENT_FAIL_ERROR_CODES)
        .values('campaign_id').distinct().count()
    )
    if strikes >= threshold:
        _suppress_phone(phone, PhoneSuppression.Reason.BOUNCE)


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


def is_plausible_e164(phone: str) -> bool:
    """True if `phone` looks like a valid E.164 number worth sending to.

    normalize_phone() is permissive — it passes malformed input through as '+<digits>'
    (e.g. a number that already had a country code gets a stray '+1' prepended → '+111…')
    and Twilio then rejects it with Error 21211, which needlessly dings the account's
    compliance score. This drops those before scheduling/charging. Rules: '+' followed by
    8–15 digits (E.164 max is 15), and a US/CA '+1' number must be exactly 11 digits total
    (+1 plus 10). Expects an already-normalized number (see normalize_phone)."""
    if not phone or not phone.startswith('+'):
        return False
    digits = phone[1:]
    if not digits.isdigit() or not (8 <= len(digits) <= 15):
        return False
    if digits.startswith('1') and len(digits) != 11:
        return False
    return True


# Explicit opt-out phrasing ("Reply STOP", "Text STOP anytime", …). Only this
# suppresses the auto-appended footer — a casual "stop by the bar" must not strip
# the compliance disclosure. Mirrored in campaign_form.html's live meter.
_OPT_OUT_RE = re.compile(r'\b(?:reply|text|send)\s+["“]?STOP\b', re.IGNORECASE)


# Common non-GSM-7 characters mapped to GSM-7-safe equivalents. These substitutions are
# semantically invisible (an em-dash reads the same as a hyphen) but keep a message in
# GSM-7 encoding, where a segment holds 160 chars instead of UCS-2's 70 — a single stray
# "smart quote" or em-dash otherwise TRIPLES the cost of a short message. Deliberate
# non-GSM-7 content (emoji, non-Latin scripts) is intentionally left untouched: if an org
# writes with emoji it accepts UCS-2; we only strip the *accidental* cost inflators.
_GSM7_TRANSLATIONS = {
    0x2010: '-', 0x2011: '-', 0x2012: '-', 0x2013: '-', 0x2014: '-', 0x2015: '-',  # dashes
    0x2022: '-', 0x00B7: '-',                                                        # bullets
    0x2018: "'", 0x2019: "'", 0x201A: "'", 0x2032: "'", 0x02BC: "'", 0x0060: "'",   # single quotes
    0x201C: '"', 0x201D: '"', 0x201E: '"', 0x2033: '"', 0x00AB: '"', 0x00BB: '"',   # double quotes
    0x2026: '...',                                                                   # ellipsis
    0x00A0: ' ', 0x202F: ' ', 0x2009: ' ', 0x2007: ' ',                             # exotic spaces
    0x200B: '', 0x200C: '', 0x200D: '', 0xFEFF: '',                                  # zero-width
}


def normalize_to_gsm7(body: str) -> str:
    """Transliterate accidental non-GSM-7 punctuation to keep a message single-segment.

    Maps smart quotes, em/en dashes, ellipses, and exotic whitespace to their GSM-7
    equivalents, and drops stray control characters (which some models emit and which
    force UCS-2). Idempotent. Leaves genuine non-GSM-7 content (emoji, non-Latin text)
    in place, so an org that deliberately uses emoji still sends what it wrote.
    """
    if not body:
        return body
    text = body.translate(_GSM7_TRANSLATIONS)
    # Drop control characters other than newlines (e.g. a stray \x1e that would force
    # UCS-2 and render as garbage); normalize tabs to spaces.
    out = []
    for ch in text:
        if ch in '\n\r' or ord(ch) >= 0x20:
            out.append(ch)
        elif ch == '\t':
            out.append(' ')
    return ''.join(out)


# Emoji and pictographic symbol ranges. Emoji force a message into UCS-2 encoding
# (70-char segments instead of 160), so a single emoji can triple the cost of a short
# message — see strip_emoji. Kept deliberately broad but scoped to symbol planes.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"      # emoji & pictographs (incl. 🎶 🎤 🌸)
    "\U00002600-\U000027BF"      # misc symbols & dingbats (☀ ✨ ✔ …)
    "\U00002B00-\U00002BFF"      # misc symbols & arrows (⭐ …)
    "\U0001F1E6-\U0001F1FF"      # regional indicators (flags)
    "\U0000FE00-\U0000FE0F"      # variation selectors
    "\U0000200D"                 # zero-width joiner (emoji sequences)
    "]+",
    flags=re.UNICODE,
)
# NOTE: the generic Arrows (U+2190–21FF) and Miscellaneous Technical (U+2300–23FF)
# blocks are deliberately EXCLUDED — they hold everyday punctuation like → and ⌘ that
# organizers use as plain text, not emoji. Including them made contains_emoji() treat a
# lone arrow as emoji (disabling emoji-stripping for the whole plan) and made strip_emoji
# delete legitimate arrows from copy.

# A model-authored opt-out footer as the TRAILING clause of a message
# ("... Reply STOP to opt out."). The compliance footer is appended automatically by
# apply_stop_footer; when the copy writes its own we strip it so it isn't baked into
# stored/sent bodies (it wastes the segment budget and disobeys the strategist
# instructions). Requires BOTH the "reply/text/send STOP" phrasing AND explicit opt-out
# context (opt out / unsubscribe / cancel / stop receiving), and must sit at end-of-string
# within a single sentence ([^.\n]* — never crosses a period or newline). This keeps it
# from eating legitimate copy: "Text STOP by the merch booth for a sticker!" and
# "Reply STOP to opt out. See you there!" are both left untouched.
_AUTHORED_FOOTER_RE = re.compile(
    r'[\s\-–—]*(?:reply|text|send)\s+["“]?stop["”]?\b[^.\n]*?'
    r'(?:opt[\s-]?out|unsubscrib\w*|cancel|stop receiving)[^.\n]*\.?\s*$',
    re.IGNORECASE,
)


def contains_emoji(text: str) -> bool:
    """True if the text contains any emoji / pictographic symbol."""
    return bool(text) and bool(_EMOJI_RE.search(text))


def strip_emoji(text: str) -> str:
    """Remove emoji and tidy the whitespace/punctuation left behind.

    Used to enforce an org's own no-emoji brand voice on AI-drafted copy, keeping the
    message in GSM-7 (one segment) instead of the costlier UCS-2 an emoji forces.
    """
    if not text:
        return text
    out = _EMOJI_RE.sub('', text)
    out = re.sub(r'[ \t]{2,}', ' ', out)          # collapse doubled spaces from removal
    out = re.sub(r'[ \t]+([.!?,;:])', r'\1', out)  # no space before punctuation
    return out.strip()


def strip_authored_stop_footer(body: str) -> str:
    """Drop a self-authored 'Reply STOP…' footer from the end of a message body.

    Leaves the rest intact; the canonical footer is re-applied at send time by
    apply_stop_footer. Returns the body unchanged when there's no trailing opt-out clause.
    """
    if not body:
        return body
    return _AUTHORED_FOOTER_RE.sub('', body).rstrip()


def apply_stop_footer(body: str, *, include: bool = True):
    """Return ``(final_body, disclosure_present)``.

    The body is first normalized to GSM-7 (:func:`normalize_to_gsm7`) so the segment
    count computed here matches what is actually sent — every send and every cost
    estimate flows through this chokepoint. If the body already carries explicit
    opt-out phrasing it is left untouched (aside from normalization) and
    ``disclosure_present`` is True (the copy itself discloses — never double-append).
    Otherwise the footer is appended only when ``include`` is True;
    ``disclosure_present`` mirrors whether the outgoing text carries a disclosure.

    Conditional inclusion drives the disclosure cadence: include the footer on the
    first message to a phone and periodically after (see SMS_FOOTER_DISCLOSURE_DAYS),
    not on every message. Twilio's Messaging Service enforces STOP regardless of this
    text, so omitting it for an already-disclosed recipient does not break opt-out."""
    body = normalize_to_gsm7(body or '')
    if _OPT_OUT_RE.search(body):
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
