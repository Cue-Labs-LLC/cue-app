from datetime import timedelta

from celery import shared_task
from celery.utils.log import get_task_logger

logger = get_task_logger(__name__)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_otp_email_task(self, otp_id):
    """Send an OTP verification email."""
    from django.core.mail import send_mail
    from django.template.loader import render_to_string
    from django.conf import settings
    from tickets.models import EmailOTP

    try:
        otp = EmailOTP.objects.get(id=otp_id)
    except EmailOTP.DoesNotExist:
        logger.warning("OTP %s not found, skipping email", otp_id)
        return

    if otp.is_verified or otp.is_expired():
        return

    context = {'otp_code': otp.otp_code, 'expiry_minutes': 10}
    html_body = render_to_string('tickets/auth/otp_email.html', context)
    text_body = render_to_string('tickets/auth/otp_email.txt', context)

    try:
        send_mail(
            subject='Your Cue verification code',
            message=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[otp.email],
            html_message=html_body,
        )
    except Exception as exc:
        logger.exception("Failed to send OTP email to %s", otp.email)
        raise self.retry(exc=exc)



@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_survey_emails_task(self, event_id, organization_id):
    """Send survey emails to all unsent invitations for an event."""
    from django.core.mail import send_mail
    from django.template.loader import render_to_string
    from django.conf import settings
    from tickets.models import SurveyInvitation, Event

    try:
        event = Event.objects.select_related('venue').get(id=event_id)
    except Event.DoesNotExist:
        logger.warning("Event %s not found, skipping survey emails", event_id)
        return

    invitations = SurveyInvitation.objects.filter(
        event_id=event_id,
        organization_id=organization_id,
        sent_at__isnull=True,
    )

    site_url = settings.SITE_URL.rstrip('/')
    sent_count = 0

    for invitation in invitations:
        survey_url = f"{site_url}/survey/{invitation.token}/"
        context = {
            'event': event,
            'survey_url': survey_url,
        }
        html_body = render_to_string('tickets/survey/survey_email.html', context)
        text_body = render_to_string('tickets/survey/survey_email.txt', context)

        try:
            send_mail(
                subject=f"How was {event.name}? Share your feedback",
                message=text_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[invitation.email],
                html_message=html_body,
            )
            from django.utils import timezone
            invitation.sent_at = timezone.now()
            invitation.save(update_fields=['sent_at'])
            sent_count += 1
        except Exception:
            logger.exception(
                "Failed to send survey email to %s for event %s",
                invitation.email, event_id,
            )

    logger.info("Sent %d survey emails for event %s", sent_count, event_id)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_org_invite_email_task(self, invitation_id):
    """Send organization invitation email with accept link."""
    from django.core.mail import send_mail
    from django.template.loader import render_to_string
    from django.conf import settings
    from tickets.models import OrganizationInvitation

    try:
        invitation = OrganizationInvitation.objects.select_related('organization').get(id=invitation_id)
    except OrganizationInvitation.DoesNotExist:
        logger.warning("Organization invitation %s not found, skipping email", invitation_id)
        return

    if not invitation.is_usable():
        return

    site_url = settings.SITE_URL.rstrip('/')
    accept_url = f"{site_url}/invite/{invitation.token}/"
    context = {
        'organization_name': invitation.organization.name,
        'accept_url': accept_url,
    }
    html_body = render_to_string('tickets/org_invite_email.html', context)
    text_body = render_to_string('tickets/org_invite_email.txt', context)

    try:
        send_mail(
            subject=f"You're invited to join {invitation.organization.name} on Cue",
            message=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[invitation.email],
            html_message=html_body,
        )
    except Exception as exc:
        logger.exception("Failed to send org invite email to %s", invitation.email)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_order_confirmation_email_task(self, order_id):
    """Send an order confirmation email to the customer."""
    from email.mime.image import MIMEImage

    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string
    from django.conf import settings
    from tickets.models import TicketOrder
    from tickets.utils import generate_qr_png_bytes

    try:
        order = TicketOrder.objects.select_related(
            'customer', 'event', 'event__venue'
        ).prefetch_related('tickets').get(id=order_id)
    except TicketOrder.DoesNotExist:
        logger.warning("TicketOrder %s not found, skipping confirmation email", order_id)
        return

    customer = order.customer
    qr_png_bytes = generate_qr_png_bytes(order.order_number)
    context = {
        'order': order,
        'customer': customer,
        'event': order.event,
        'tickets': list(order.tickets.all()),
        'show_qr_code': bool(qr_png_bytes),
    }
    html_body = render_to_string('tickets/buy/order_confirmation_email.html', context)
    text_body = render_to_string('tickets/buy/order_confirmation_email.txt', context)

    subject = f"Your order confirmation - {order.event.name}"
    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[customer.email],
    )
    msg.attach_alternative(html_body, 'text/html')
    if qr_png_bytes:
        qr_attachment = MIMEImage(qr_png_bytes)
        qr_attachment.add_header('Content-ID', '<qrcode>')
        qr_attachment.add_header('Content-Disposition', 'inline', filename='qrcode.png')
        msg.attach(qr_attachment)

    try:
        msg.send()
        logger.info("Sent order confirmation email for order %s to %s", order_id, customer.email)
    except Exception as exc:
        logger.exception("Failed to send order confirmation email for order %s", order_id)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def recalculate_rfm_task(self, organization_id):
    from tickets.models import Organization
    from tickets.services.segmentation.rfm_calculator import RFMCalculator

    org = Organization.objects.get(id=organization_id)
    try:
        org.rfm_recalc_in_progress = True
        org.save(update_fields=["rfm_recalc_in_progress"])
        RFMCalculator(org).calculate_all()
    except Exception as exc:
        logger.exception("RFM recalc failed for org %s", organization_id)
        raise self.retry(exc=exc)
    finally:
        org.rfm_recalc_in_progress = False
        org.save(update_fields=["rfm_recalc_in_progress"])


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def notify_next_waitlist_entry(self, ticket_type_id):
    """Notify the next person on a waitlist that a spot is available."""
    from django.core.mail import send_mail
    from django.template.loader import render_to_string
    from django.conf import settings
    from django.db import transaction
    from django.db.models import F
    from django.utils import timezone as tz
    from tickets.models import SaleableTicketType, WaitlistEntry

    try:
        with transaction.atomic():
            tt = SaleableTicketType.objects.select_for_update().get(id=ticket_type_id)
    except SaleableTicketType.DoesNotExist:
        return

    if not tt.waitlist_enabled:
        return

    # Expire any stale holds before checking availability — makes the chain
    # self-healing even if expire_waitlist_hold never ran (e.g., dev eager mode,
    # worker restart).
    from django.db.models import Value
    from django.db.models.functions import Greatest

    stale_qs = WaitlistEntry.objects.filter(
        ticket_type=tt,
        notified_at__isnull=False,
        hold_expires_at__lt=tz.now(),
        purchased_at__isnull=True,
        expired=False,
    )
    for stale in stale_qs:
        stale.expired = True
        stale.save(update_fields=['expired'])
        SaleableTicketType.objects.filter(id=ticket_type_id).update(
            quantity_held=Greatest(F('quantity_held') - 1, Value(0))
        )

    if tt.quantity_limit is not None:
        tt.refresh_from_db(fields=['quantity_held'])
        if (tt.quantity_sold + tt.quantity_held) >= tt.quantity_limit:
            return

    entry = (WaitlistEntry.objects
             .filter(ticket_type=tt, notified_at__isnull=True, expired=False, purchased_at__isnull=True)
             .order_by('position')
             .first())
    if not entry:
        return

    expires = tz.now() + timedelta(minutes=10)
    with transaction.atomic():
        entry.notified_at = tz.now()
        entry.hold_expires_at = expires
        entry.save(update_fields=['notified_at', 'hold_expires_at'])
        SaleableTicketType.objects.filter(id=ticket_type_id).update(
            quantity_held=F('quantity_held') + 1
        )

    site_url = getattr(settings, 'SITE_URL', '').rstrip('/')
    activate_url = f"{site_url}/e/{tt.event.public_id}/waitlist/activate/{entry.hold_token}/"
    context = {
        'entry': entry,
        'ticket_type': tt,
        'event': tt.event,
        'activate_url': activate_url,
    }
    try:
        send_mail(
            subject=f'Your spot is ready - {tt.event.name}',
            message=render_to_string('tickets/buy/waitlist_notification_email.txt', context),
            html_message=render_to_string('tickets/buy/waitlist_notification_email.html', context),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[entry.email],
            fail_silently=False,
        )
    except Exception as exc:
        logger.exception("Failed to send waitlist notification to %s", entry.email)
        raise self.retry(exc=exc)

    expire_waitlist_hold.apply_async(args=[str(entry.id)], countdown=610)
    logger.info("Waitlist: notified %s for ticket type %s (hold expires %s)", entry.email, ticket_type_id, expires)


@shared_task(bind=True, max_retries=1)
def expire_waitlist_hold(self, entry_id):
    """Expire a waitlist hold that was not converted to a purchase."""
    from django.db import transaction
    from django.db.models import F
    from django.db.models.functions import Greatest
    from django.db.models import Value
    from django.utils import timezone as tz
    from tickets.models import WaitlistEntry, SaleableTicketType

    try:
        entry = WaitlistEntry.objects.select_related('ticket_type').get(id=entry_id)
    except WaitlistEntry.DoesNotExist:
        return

    if entry.purchased_at is not None or entry.expired:
        return

    if entry.hold_expires_at and tz.now() < entry.hold_expires_at:
        return

    with transaction.atomic():
        entry.expired = True
        entry.save(update_fields=['expired'])
        SaleableTicketType.objects.filter(id=entry.ticket_type_id).update(
            quantity_held=Greatest(F('quantity_held') - 1, Value(0))
        )

    notify_next_waitlist_entry.delay(str(entry.ticket_type_id))
    logger.info("Waitlist: hold expired for entry %s, notifying next", entry_id)
