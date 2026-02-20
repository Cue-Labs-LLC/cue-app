import logging
from django.conf import settings

logger = logging.getLogger(__name__)


def send_sms(to_phone: str, message: str) -> bool:
    """Send SMS via Twilio. Returns True on success, False on failure."""
    try:
        from twilio.rest import Client
        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        client.messages.create(
            body=message,
            from_=settings.TWILIO_PHONE_NUMBER,
            to=to_phone,
        )
        return True
    except Exception as exc:
        logger.error("SMS send failed to %s: %s", to_phone, exc)
        return False
