from datetime import timedelta
from decimal import Decimal

from celery import shared_task
from celery.utils.log import get_task_logger

logger = get_task_logger(__name__)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_receipt_email_task(self, receipt_send_id, fallback_event_id=''):
    """Dispatch a queued scanner receipt email.

    The ReceiptSend row is created by /api/scanner/receipt/ in 'queued' state.
    If it has a ticket_order we send the standard order confirmation; otherwise
    we re-retrieve the PaymentIntent and send a transaction-attempt summary
    (Apple §5.10).

    Updates ReceiptSend.status to 'sent' or 'failed' in place — used for the
    audit trail surfaced via /api/scanner/receipt-status/ (future) and Apple
    review compliance.
    """
    from datetime import datetime, timezone as _tz
    from django.conf import settings

    from tickets.api_views import (
        _send_receipt_email_for_order,
        _send_receipt_email_for_intent,
    )
    from tickets.models import Event, ReceiptSend, TicketOrder

    try:
        rs = ReceiptSend.objects.select_related('organization', 'ticket_order').get(id=receipt_send_id)
    except ReceiptSend.DoesNotExist:
        logger.warning("ReceiptSend %s not found, skipping", receipt_send_id)
        return

    try:
        if rs.ticket_order_id:
            order = (
                TicketOrder.objects
                .select_related('customer', 'event', 'event__venue', 'stripe_checkout_session')
                .prefetch_related('tickets')
                .get(id=rs.ticket_order_id)
            )
            status, error_message = _send_receipt_email_for_order(order, rs.contact)
        else:
            import stripe as stripe_lib
            stripe_lib.api_key = settings.STRIPE_SECRET_KEY
            retrieve_kwargs = {}
            if rs.organization.stripe_account_id:
                retrieve_kwargs['stripe_account'] = rs.organization.stripe_account_id
            try:
                pi = stripe_lib.PaymentIntent.retrieve(rs.payment_intent_id, **retrieve_kwargs)
            except Exception as exc:
                logger.exception("Stripe PaymentIntent retrieve failed in receipt task")
                rs.status = 'failed'
                rs.error_message = str(exc)[:1000]
                rs.save(update_fields=['status', 'error_message', 'updated_at'])
                return

            pi_event = None
            try:
                metadata = getattr(pi, 'metadata', None)
                pi_event_id = metadata.get('event_id') if metadata is not None and hasattr(metadata, 'get') else None
            except Exception:
                pi_event_id = None
            if pi_event_id:
                pi_event = Event.objects.filter(organization=rs.organization, id=pi_event_id).first()
            if pi_event is None and fallback_event_id:
                pi_event = Event.objects.filter(organization=rs.organization, id=fallback_event_id).first()

            status_raw = (getattr(pi, 'status', '') or '').lower()
            status_labels = {
                'succeeded': 'Approved',
                'canceled': 'Cancelled',
                'cancelled': 'Cancelled',
                'requires_payment_method': 'Declined',
                'requires_confirmation': 'Incomplete',
                'requires_action': 'Incomplete',
                'requires_capture': 'Authorized',
                'processing': 'Processing',
            }
            status_label = status_labels.get(status_raw, status_raw.replace('_', ' ').title() or 'Unknown')

            decline_message = ''
            try:
                lpe = getattr(pi, 'last_payment_error', None)
                if lpe is not None:
                    decline_message = (
                        getattr(lpe, 'message', None)
                        or (lpe.get('message', '') if isinstance(lpe, dict) else '')
                        or ''
                    )
            except Exception:
                decline_message = ''

            succeeded = status_raw == 'succeeded'
            summary = {
                'merchant_business_name': rs.organization.name,
                'event_name': pi_event.name if pi_event else '',
                'amount': (pi.amount / 100.0) if getattr(pi, 'amount', None) is not None else None,
                'currency': (getattr(pi, 'currency', '') or '').upper(),
                'created': datetime.fromtimestamp(pi.created, tz=_tz.utc) if getattr(pi, 'created', None) else None,
                'status_label': status_label,
                'decline_message': decline_message,
                'succeeded': succeeded,
                'note': '' if succeeded else 'No charge was made to your card.',
                'payment_intent_id': getattr(pi, 'id', rs.payment_intent_id),
            }
            status, error_message = _send_receipt_email_for_intent(summary, rs.contact)
    except Exception as exc:
        logger.exception("Receipt email task crashed for ReceiptSend %s", receipt_send_id)
        try:
            rs.status = 'failed'
            rs.error_message = str(exc)[:1000]
            rs.save(update_fields=['status', 'error_message', 'updated_at'])
        except Exception:
            pass
        raise self.retry(exc=exc)

    rs.status = status
    rs.error_message = error_message
    rs.save(update_fields=['status', 'error_message', 'updated_at'])


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



def build_survey_email(event, survey_url):
    """Render the survey invitation email as (subject, text_body, html_body).
    Shared by the bulk send task and the builder's test-send."""
    from django.template.loader import render_to_string
    # Masthead/sender name mirrors the From-line opt-in: when the org sets a
    # reply-to, the email presents as the organizer; otherwise it's "Cue".
    org = event.organization
    sender_name = org.name if (org.survey_reply_to_email or '').strip() else 'Cue'
    context = {'event': event, 'survey_url': survey_url, 'sender_name': sender_name}
    return (
        event.resolved_survey_subject(),
        render_to_string('tickets/survey/survey_email.txt', context),
        render_to_string('tickets/survey/survey_email.html', context),
    )


def survey_sender_fields(organization):
    """(from_email, reply_to) for an org's survey mail.

    Opt-in: when survey_reply_to_email is set, show the org name on the From line
    (keeping Cue's verified sending address so SPF/DKIM/DMARC still pass) and route
    replies to the organizer. Blank = today's behavior: Cue default sender, no
    reply-to. Shared by the bulk send task and the builder's test-send."""
    from django.conf import settings
    from email.utils import parseaddr, formataddr
    reply_to = (organization.survey_reply_to_email or '').strip()
    if not reply_to:
        return settings.DEFAULT_FROM_EMAIL, None
    _, address = parseaddr(settings.DEFAULT_FROM_EMAIL)  # e.g. noreply@cueup.co
    return formataddr((organization.name, address)), [reply_to]


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_survey_emails_task(self, event_id, organization_id):
    """Send survey emails for an event's due, unsent invitations.

    Only sends rows whose scheduled_send_at is NULL (send-now) or already in the
    past, so scheduled invitations sit untouched until the send_due_survey_invitations
    cron dispatches them once due.
    """
    import smtplib

    from django.core.mail import EmailMultiAlternatives
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email
    from django.conf import settings
    from django.db.models import Q
    from django.utils import timezone
    from tickets.models import SurveyInvitation, Event

    try:
        event = Event.objects.select_related('venue', 'organization').get(id=event_id)
    except Event.DoesNotExist:
        logger.warning("Event %s not found, skipping survey emails", event_id)
        return

    invitations = SurveyInvitation.objects.filter(
        event_id=event_id,
        organization_id=organization_id,
        sent_at__isnull=True,
        send_failed_at__isnull=True,
    ).filter(
        Q(scheduled_send_at__isnull=True) | Q(scheduled_send_at__lte=timezone.now())
    )

    site_url = settings.SITE_URL.rstrip('/')
    from_email, reply_to = survey_sender_fields(event.organization)
    sent_count = 0

    def mark_permanent_failure(invitation, reason):
        """Mark an invitation as permanently unsendable so it drops out of the
        eligibility query and is never retried."""
        invitation.send_failed_at = timezone.now()
        invitation.send_error = reason[:200]
        invitation.save(update_fields=['send_failed_at', 'send_error'])

    for invitation in invitations:
        # Reject obviously-invalid recipients (e.g. Apple "Hide My Email" placeholder
        # text that slipped into the customer record) before hitting SMTP.
        try:
            validate_email(invitation.email)
        except ValidationError:
            logger.warning(
                "Skipping survey invitation %s: invalid email %r for event %s",
                invitation.id, invitation.email, event_id,
            )
            mark_permanent_failure(invitation, 'invalid_email')
            continue

        survey_url = f"{site_url}/survey/{invitation.token}/"
        subject, text_body, html_body = build_survey_email(event, survey_url)

        try:
            msg = EmailMultiAlternatives(
                subject=subject,
                body=text_body,
                from_email=from_email,
                to=[invitation.email],
                reply_to=reply_to,
            )
            msg.attach_alternative(html_body, "text/html")
            msg.send()
            invitation.sent_at = timezone.now()
            invitation.save(update_fields=['sent_at'])
            sent_count += 1
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
            # smtplib raises these for ANY non-250 reply, including transient 4xx
            # codes (421 rate limit, 450 greylisting). Only a 5xx reply means
            # retrying will always fail — mark those rows so they stop looping;
            # leave 4xx unsent for a future dispatch.
            if isinstance(exc, smtplib.SMTPSenderRefused):
                codes = [exc.smtp_code]
            else:
                codes = [code for code, _resp in exc.recipients.values()]
            if codes and all(code >= 500 for code in codes):
                logger.warning(
                    "Survey email to %s for event %s permanently refused: %s",
                    invitation.email, event_id, exc,
                )
                mark_permanent_failure(invitation, 'recipient_refused')
            else:
                logger.warning(
                    "Survey email to %s for event %s temporarily refused, will retry: %s",
                    invitation.email, event_id, exc,
                )
        except Exception:
            # Transient failure (SMTP timeout, connection reset, etc.) — log and leave
            # the row unsent so a future dispatch can retry it.
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
    from tickets.utils import build_ticket_qr_codes

    try:
        order = TicketOrder.objects.select_related(
            'customer', 'event', 'event__venue'
        ).prefetch_related('tickets').get(id=order_id)
    except TicketOrder.DoesNotExist:
        logger.warning("TicketOrder %s not found, skipping confirmation email", order_id)
        return

    customer = order.customer
    tickets = list(order.tickets.all())
    ticket_qrs = build_ticket_qr_codes(tickets)

    from decimal import Decimal
    try:
        service_fee = Decimal(order.stripe_checkout_session.platform_fee_cents) / 100
    except Exception:
        service_fee = Decimal('0.00')

    context = {
        'order': order,
        'customer': customer,
        'event': order.event,
        'tickets': tickets,
        'ticket_qrs': ticket_qrs,
        'show_qr_code': bool(ticket_qrs),
        'service_fee': service_fee,
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
    for qr in ticket_qrs:
        qr_attachment = MIMEImage(qr['png_bytes'])
        qr_attachment.add_header('Content-ID', f"<{qr['cid']}>")
        qr_attachment.add_header('Content-Disposition', 'inline', filename=f"{qr['cid']}.png")
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
    from tickets.services.segmentation import recalculate_customer_segments

    org = Organization.objects.get(id=organization_id)
    try:
        org.rfm_recalc_in_progress = True
        org.save(update_fields=["rfm_recalc_in_progress"])
        recalculate_customer_segments(org)
    except Exception as exc:
        logger.exception("RFM recalc failed for org %s", organization_id)
        raise self.retry(exc=exc)
    finally:
        org.rfm_recalc_in_progress = False
        org.save(update_fields=["rfm_recalc_in_progress"])


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def recalculate_loyalty_tiers_task(self, program_id):
    """Reassign every customer to the best tier of an ACTIVE loyalty program.

    Hardened against the failure modes found in review:
      - gated: only runs for a live (active, non-deleted) program, so recalc of
        an inactive/second program can never clobber the active one's members;
      - serialized: claims the program row under select_for_update and a
        recalc_in_progress flag, so overlapping clicks/workers/retries don't
        interleave partial writes;
      - atomic: the whole reassignment commits in one transaction — a mid-run
        failure rolls back instead of leaving half the org reassigned;
      - honest: last_recalculated_at is stamped only on success.
    """
    from django.db import transaction
    from django.utils import timezone
    from tickets.models import LoyaltyProgram
    from tickets.services.loyalty import assign_loyalty_tiers

    # Claim the job: lock the row, verify it's live and not already running.
    try:
        with transaction.atomic():
            program = (
                LoyaltyProgram.objects.select_for_update(of=('self',))
                .select_related('organization')
                .get(id=program_id, deleted_at__isnull=True)
            )
            if not program.organization.loyalty_feature_enabled:
                logger.info("Loyalty feature disabled for org of program %s, skipping tier recalc", program_id)
                return 0
            if not program.is_active:
                logger.info("LoyaltyProgram %s is inactive, skipping tier recalc", program_id)
                return 0
            if program.recalc_in_progress:
                logger.info("LoyaltyProgram %s recalc already in progress, skipping", program_id)
                return 0
            program.recalc_in_progress = True
            program.save(update_fields=["recalc_in_progress"])
    except LoyaltyProgram.DoesNotExist:
        logger.warning("LoyaltyProgram %s not found, skipping tier recalc", program_id)
        return 0

    success = False
    try:
        with transaction.atomic():
            assigned = assign_loyalty_tiers(program)
        success = True
        return assigned
    except Exception as exc:
        logger.exception("Loyalty tier recalc failed for program %s", program_id)
        raise self.retry(exc=exc)
    finally:
        update_fields = ["recalc_in_progress"]
        program.recalc_in_progress = False
        if success:
            program.last_recalculated_at = timezone.now()
            update_fields.append("last_recalculated_at")
        program.save(update_fields=update_fields)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def backfill_loyalty_points_task(self, program_id, reset_first=False):
    """Award loyalty points for an org's historical orders, then recalc tiers.

    Idempotent end-to-end (one EARN per order, DB-enforced), which makes this
    task double as the REPAIR path: re-running it heals any award that a hook
    swallowed. Claims the program's recalc_in_progress flag for its duration so
    manual recalcs can't assign tiers from a partial ledger — and clears the
    flag BEFORE enqueueing the chained recalc (which checks the same flag and
    would otherwise skip itself).

    ``reset_first=True`` powers the organizer "Recompute points" action: it wipes
    the org's ledger + balances FIRST so every order is re-awarded at the CURRENT
    basis/rate (a plain re-run skips already-earned orders), then runs a final
    ledger-driven reconciliation so a purchase that interleaves with the wipe
    can't leave a balance behind. Both the reset and the reconcile live inside
    the claim window and the try, so a failure clears the flag and retries clean.
    """
    from django.db import transaction
    from django.utils import timezone
    from tickets.models import LoyaltyProgram, TicketOrder
    from tickets.services.loyalty import (
        award_points_for_orders,
        reconcile_points_balances,
        reset_points_for_organization,
    )

    CHUNK = 500

    try:
        with transaction.atomic():
            program = (
                LoyaltyProgram.objects.select_for_update(of=('self',))
                .select_related('organization')
                .get(id=program_id, deleted_at__isnull=True)
            )
            if not program.organization.loyalty_feature_enabled:
                logger.info("Loyalty feature disabled for org of program %s, skipping backfill", program_id)
                return 0
            if not program.is_active or not program.points_enabled:
                logger.info("LoyaltyProgram %s inactive or points disabled, skipping backfill", program_id)
                return 0
            if program.recalc_in_progress:
                logger.info("LoyaltyProgram %s busy (recalc/backfill in progress), skipping backfill", program_id)
                return 0
            program.recalc_in_progress = True
            program.save(update_fields=["recalc_in_progress"])
    except LoyaltyProgram.DoesNotExist:
        logger.warning("LoyaltyProgram %s not found, skipping points backfill", program_id)
        return 0

    org = program.organization
    total = 0
    try:
        # Recompute mode: wipe the org's ledger + balances so every order is
        # re-awarded at the CURRENT rate (a plain backfill skips already-earned
        # orders). Inside the try + claim window so a failure retries clean.
        if reset_first:
            reset_points_for_organization(org)

        orders_qs = (
            TicketOrder.objects.filter(
                customer__organization=org,
                refunded_at__isnull=True,
                deleted_at__isnull=True,
            )
            .exclude(customer__email__endswith='@placeholder.local')
            .select_related('customer')
        )
        chunk = []
        for order in orders_qs.iterator(chunk_size=CHUNK):
            chunk.append(order)
            if len(chunk) >= CHUNK:
                total += award_points_for_orders(chunk, program, description='Historical backfill')
                chunk = []
        if chunk:
            total += award_points_for_orders(chunk, program, description='Historical backfill')

        # Race-closer: after a recompute, set every balance = SUM(ledger) so a
        # purchase that interleaved with the wipe is reconciled to ledger truth.
        if reset_first:
            reconcile_points_balances(org)

        org.loyalty_points_backfilled_at = timezone.now()
        org.save(update_fields=['loyalty_points_backfilled_at'])
    except Exception as exc:
        logger.exception("Loyalty points backfill failed for program %s", program_id)
        program.recalc_in_progress = False
        program.save(update_fields=["recalc_in_progress"])
        raise self.retry(exc=exc)

    # Clear the claim BEFORE chaining the recalc — it checks the same flag.
    program.recalc_in_progress = False
    program.save(update_fields=["recalc_in_progress"])
    recalculate_loyalty_tiers_task.delay(str(program.id))
    return total


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def generate_org_ai_opportunities_task(self, organization_id):
    """Generate reviewed AI recommendations for one organization."""
    from tickets.models import Organization
    from tickets.services.ai_recommendations import AIRecommendationGenerator

    try:
        org = Organization.objects.get(id=organization_id)
    except Organization.DoesNotExist:
        logger.warning("Organization %s not found, skipping AI opportunities", organization_id)
        return 0

    try:
        recommendations = AIRecommendationGenerator(org).run_all()
    except Exception as exc:
        logger.exception("AI opportunity generation failed for org %s", organization_id)
        raise self.retry(exc=exc)

    logger.info(
        "Generated/updated %d AI recommendations for org %s",
        len(recommendations),
        organization_id,
    )
    return len(recommendations)


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


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def process_csv_task(self, uploaded_file_id, manual_prices=None, tier_definitions=None):
    """Process an uploaded CSV file asynchronously.

    manual_prices and tier_definitions are JSON-safe (Decimal values serialized as strings).
    """
    from tickets.models import UploadedFile
    from tickets.csv_processor import CSVProcessor
    from tickets.services.segmentation import recalculate_customer_segments
    from tickets.views import _invalidate_event_list_cache

    try:
        uploaded_file = UploadedFile.objects.select_related(
            'organization', 'csv_format'
        ).get(id=uploaded_file_id)
    except UploadedFile.DoesNotExist:
        logger.warning("process_csv_task: UploadedFile %s not found", uploaded_file_id)
        return

    # Deserialize Decimal values (serialized as strings for JSON transport)
    if manual_prices:
        manual_prices = {k: Decimal(v) for k, v in manual_prices.items()}
    if tier_definitions:
        for tiers in tier_definitions.values():
            for tier in tiers:
                if tier.get('price') is not None:
                    tier['price'] = Decimal(str(tier['price']))

    if not uploaded_file.csv_file:
        uploaded_file.status = 'failed'
        uploaded_file.metadata['processing_results'] = {'error': 'No stored file found.'}
        uploaded_file.save(update_fields=['status', 'metadata'])
        return

    processor = CSVProcessor(uploaded_file, uploaded_file.csv_format)
    file_handle = uploaded_file.csv_file.open('rb')
    try:
        is_valid, error_msg = processor.validate_csv(file_handle)
        if not is_valid:
            uploaded_file.status = 'failed'
            uploaded_file.metadata['processing_results'] = {'error': error_msg}
            uploaded_file.save(update_fields=['status', 'metadata'])
            return

        results = processor.process_and_save(
            file_handle,
            manual_prices=manual_prices,
            tier_definitions=tier_definitions,
        )
    except Exception as exc:
        uploaded_file.status = 'failed'
        uploaded_file.save(update_fields=['status'])
        logger.exception("process_csv_task failed for %s", uploaded_file_id)
        raise self.retry(exc=exc)
    finally:
        file_handle.close()

    uploaded_file.metadata['processing_results'] = {
        'success_count': results['success_count'],
        'error_count': results['error_count'],
        'skipped_duplicates': results['skipped_duplicates'],
        'errors': results['errors'][:50],
        'skipped_order_numbers': results['skipped_order_numbers'][:50],
        'rejected_orders': results.get('rejected_orders', [])[:50],
        'skipped_rows_count': results.get('skipped_rows_count', 0),
        'skipped_rows_by_reason': results.get('skipped_rows_by_reason', {}),
    }
    uploaded_file.save(update_fields=['metadata'])

    if results['success_count'] > 0:
        try:
            recalculate_customer_segments(uploaded_file.organization)
        except Exception:
            logger.exception("RFM recalc after CSV import failed for %s", uploaded_file_id)
        _invalidate_event_list_cache(uploaded_file.organization)

    logger.info(
        "process_csv_task complete for %s: %d success, %d errors",
        uploaded_file_id, results['success_count'], results['error_count'],
    )


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def sync_typeform_form_task(self, subscription_id):
    """Pull recent Typeform responses for one subscription and ingest them."""
    import traceback

    from django.utils import timezone

    from tickets.models import TypeformFormSubscription
    from tickets.services.typeform.client import TypeformAPIError, TypeformClient
    from tickets.services.typeform.ingest import ingest_response

    try:
        subscription = TypeformFormSubscription.objects.select_related('organization').get(
            id=subscription_id,
        )
    except TypeformFormSubscription.DoesNotExist:
        logger.warning("Typeform subscription %s not found", subscription_id)
        return

    if not subscription.is_active:
        return

    org = subscription.organization
    access_token = org.typeform_access_token
    if not access_token:
        subscription.last_sync_error = 'Organization is not connected to Typeform.'
        subscription.save(update_fields=['last_sync_error'])
        return

    started_at = timezone.now()
    client = TypeformClient(access_token=access_token)
    new_response_ids: list[str] = []

    try:
        after_token: str | None = None
        while True:
            payload = client.list_responses(
                form_id=subscription.form_id,
                since=subscription.last_synced_at,
                after=after_token,
            )
            items = payload.get('items') or []
            for item in items:
                response, created = ingest_response(subscription, item)
                if response and created and response.id:
                    new_response_ids.append(str(response.id))
            page_count = payload.get('page_count') or 0
            current_page = payload.get('page') or 1
            if current_page >= page_count or not items:
                break
            after_token = items[-1].get('token') or items[-1].get('response_id')
            if not after_token:
                break

        subscription.last_synced_at = started_at
        subscription.last_sync_error = ''
        subscription.save(update_fields=['last_synced_at', 'last_sync_error'])
    except TypeformAPIError as exc:
        subscription.last_sync_error = str(exc)
        subscription.save(update_fields=['last_sync_error'])
        logger.warning("Typeform sync failed for sub %s: %s", subscription_id, exc)
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            return
    except Exception as exc:  # noqa: BLE001
        subscription.last_sync_error = traceback.format_exc()
        subscription.save(update_fields=['last_sync_error'])
        logger.exception("Typeform sync unexpected error for sub %s", subscription_id)
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            return

    for response_id in new_response_ids:
        match_survey_response_to_event_task.delay(response_id)


@shared_task(bind=True, max_retries=1, default_retry_delay=30)
def match_survey_response_to_event_task(self, response_id):
    """Run the LLM event matcher for one survey response and save the suggestion."""
    from tickets.models import ExternalSurveyResponse
    from tickets.services.typeform.event_matcher import (
        SurveyEventMatcher, apply_top_candidate,
    )

    try:
        response = ExternalSurveyResponse.objects.select_related('organization').get(
            id=response_id,
        )
    except ExternalSurveyResponse.DoesNotExist:
        return

    if response.event_id:
        return

    try:
        matcher = SurveyEventMatcher(response.organization)
        result = matcher.suggest(response)
    except Exception:
        logger.exception("Survey-to-event match failed for %s", response_id)
        return

    apply_top_candidate(response, result)


# ---------------------------------------------------------------------------
# Native marketing SMS
#
#   send_due_sms_campaigns (Render cron, */5)  ──► send_sms_campaign_task
#                                                       │ atomic claim, snapshot
#                                                       ▼ chord fan-out
#                                                 send_sms_chunk_task × N
#                                                       ▼ chord callback
#                                                 finalize_sms_campaign_task
# ---------------------------------------------------------------------------

from tickets.sms import apply_stop_footer as _apply_stop_footer  # noqa: E402  (shared helper)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_sms_campaign_task(self, campaign_id):
    """Orchestrate a marketing SMS send: atomically claim the campaign, snapshot
    recipients (re-resolving the audience and re-checking suppression at send
    time), then fan out chunked send tasks. Idempotent and recovery-safe — a
    re-dispatch of a stuck 'sending' campaign reuses existing recipient rows and
    only sends the ones still queued."""
    from django.conf import settings
    from django.utils import timezone as tz
    from celery import chord
    from tickets.models import SMSCampaign, SMSMessageRecipient

    # Atomic claim: exactly one worker can move draft/scheduled -> sending.
    claimed = SMSCampaign.objects.filter(
        id=campaign_id,
        status__in=[SMSCampaign.Status.DRAFT, SMSCampaign.Status.SCHEDULED],
    ).update(status=SMSCampaign.Status.SENDING, started_at=tz.now())

    campaign = SMSCampaign.objects.filter(id=campaign_id).select_related(
        'organization'
    ).first()
    if not campaign:
        return
    if not claimed and campaign.status != SMSCampaign.Status.SENDING:
        # Already sent/canceled, or another worker owns it.
        logger.info("SMS campaign %s not claimable (status=%s); skipping", campaign_id, campaign.status)
        return

    org = campaign.organization

    # Snapshot recipients once. Recovery re-dispatch finds rows already present.
    if not SMSMessageRecipient.objects.filter(campaign=campaign).exists():
        cap = getattr(settings, 'SMS_CAMPAIGN_MAX_RECIPIENTS', 5000)
        recipients = campaign.materialize(org, cap=cap)
        SMSMessageRecipient.objects.bulk_create(
            [
                SMSMessageRecipient(
                    campaign=campaign, customer_id=r['customer_id'], phone=r['phone']
                )
                for r in recipients
            ],
            batch_size=500,
        )
        SMSCampaign.objects.filter(id=campaign.id).update(
            audience_size=SMSMessageRecipient.objects.filter(campaign=campaign).count()
        )

    # "Unsent" == QUEUED with no twilio_sid. A row that carries a SID was already
    # accepted by Twilio, so it must never be re-sent — even if a stale callback
    # regressed its status to QUEUED. Recovery and finalize share this predicate.
    queued = list(
        SMSMessageRecipient.objects.filter(
            campaign=campaign, status=SMSMessageRecipient.Status.QUEUED, twilio_sid=''
        ).values_list('id', 'segments')
    )
    if not queued:
        _finalize_sms_campaign(campaign.id)
        return

    # Daily carrier-cap last resort. The composer blocks oversize sends up front (day-aware),
    # so a campaign should fit its day by the time it dispatches. If a race the composer can't
    # see (simultaneous confirms, a reschedule onto a full day) leaves this day over budget,
    # fail the WHOLE campaign — never partially, never deferred, so the shared cap is never
    # exceeded — refund it, and record a reason so the organizer can shrink + reschedule.
    from tickets.services.sms_limits import remaining_daily_budget, fit_within_budget
    budget = remaining_daily_budget()
    if budget is not None and fit_within_budget([seg for _, seg in queued], budget) < len(queued):
        from tickets.services.sms_credits import refund_campaign
        reason = ('Daily send limit for this date was reached before the campaign dispatched. '
                  'Reduce the recipient list and reschedule.')
        SMSCampaign.objects.filter(id=campaign.id).update(
            status=SMSCampaign.Status.FAILED, failure_reason=reason,
        )
        refund_campaign(campaign, description='Daily send limit reached')
        logger.warning(
            "SMS campaign %s failed: daily cap reached at send time (%d recipient(s), budget %s)",
            campaign_id, len(queued), budget,
        )
        return
    queued_ids = [str(rid) for rid, _ in queued]

    # Pace dispatch to roughly SMS_SEND_RATE_PER_SEC by staggering chunk start times,
    # rather than blasting the whole audience at once (a burst gets carrier-filtered as
    # spam — Error 30007). chunk N starts at ~N*chunk_size/rate seconds. In eager mode
    # (dev) countdowns are ignored and everything runs inline.
    rate = max(1, getattr(settings, 'SMS_SEND_RATE_PER_SEC', 5))
    chunk_size = max(1, getattr(settings, 'SMS_CHUNK_SIZE', 10))
    header = []
    for idx, start in enumerate(range(0, len(queued_ids), chunk_size)):
        delay = int(idx * chunk_size / rate)
        header.append(
            send_sms_chunk_task.s(str(campaign.id), queued_ids[start:start + chunk_size])
            .set(countdown=delay)
        )
    chord(header)(finalize_sms_campaign_task.s(str(campaign.id)))


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_sms_chunk_task(self, campaign_id, recipient_ids):
    """Send one chunk of a campaign. Per-recipient failure is isolated — one bad
    number never aborts the chunk. Global throughput is paced by the orchestrator, which
    staggers chunk start times to hold ~SMS_SEND_RATE_PER_SEC (see send_sms_campaign_task)."""
    import secrets
    from django.conf import settings
    from django.urls import reverse, NoReverseMatch
    from django.utils import timezone as tz
    from tickets.models import SMSCampaign, SMSMessageRecipient, PhoneSuppression
    from tickets.sms import send_sms, TWILIO_OPT_OUT_ERROR_CODES

    campaign = SMSCampaign.objects.filter(id=campaign_id).select_related('organization').first()
    if not campaign or campaign.status == SMSCampaign.Status.CANCELED:
        return

    # The audience is frozen at schedule time, so re-check opt-out HERE: anyone who
    # replied STOP between scheduling and sending must be skipped (compliance). They
    # were charged but won't be texted — charged >= sent, never the reverse.
    suppressed = PhoneSuppression.suppressed_phones(campaign.organization)

    # Twilio must reach our URLs (status callback, tracked links) from the public
    # internet, so skip them for localhost/private hosts (dev). Status just won't
    # auto-update and links stay untracked locally; use a tunnel + public SITE_URL.
    site_url = getattr(settings, 'SITE_URL', '').rstrip('/')
    is_public = False
    if site_url and '://' in site_url:
        host = site_url.split('://', 1)[1].split('/', 1)[0].split(':')[0].lower()
        is_public = host not in ('localhost', '127.0.0.1', '0.0.0.0') and not host.endswith('.local')

    status_cb = None
    if is_public:
        try:
            status_cb = f"{site_url}{reverse('tickets:twilio_sms_status_webhook')}"
        except NoReverseMatch:
            status_cb = None

    # Rewrite the campaign's link to a per-recipient tracked link when we have a
    # public URL to point at.
    track_links = bool(campaign.link_url) and is_public

    # The STOP-footer decision was made + persisted (stop_disclosed) and billed at
    # schedule time; honor it here so charged == sent. Safeguard: if a planned-omit
    # recipient's disclosure has aged out by the time we actually dispatch (a badly
    # delayed send), re-include the footer for compliance — rare, may cost one
    # unbilled segment, and only ever errs toward disclosing.
    queued = list(SMSMessageRecipient.objects.filter(
        id__in=recipient_ids, status=SMSMessageRecipient.Status.QUEUED, twilio_sid=''
    ))
    disclosed_now = SMSMessageRecipient.recently_disclosed_phones(
        campaign.organization, [r.phone for r in queued], as_of=tz.now()
    )

    for r in queued:
        # Opted out since the audience was frozen → do not send.
        if r.phone in suppressed:
            r.status = SMSMessageRecipient.Status.FAILED
            r.error_message = 'opted out before send'
            r.save(update_fields=['status', 'error_message', 'updated_at'])
            continue

        include_footer = r.stop_disclosed
        if not include_footer and r.phone not in disclosed_now:
            logger.info(
                "STOP disclosure for %s aged out before send; re-including footer", r.phone
            )
            include_footer = True

        base_body = campaign.body
        update_fields = ['status', 'twilio_sid', 'sent_at', 'error_code', 'error_message', 'updated_at']
        if track_links:
            token = secrets.token_urlsafe(8)
            tracked = f"{site_url}{reverse('tickets:sms_click_redirect', kwargs={'token': token})}"
            base_body = campaign.body.replace(campaign.link_url, tracked, 1)
            r.click_token = token
            update_fields.append('click_token')
        body, _ = _apply_stop_footer(base_body, include=include_footer)

        ok, sid, err_code = send_sms(r.phone, body, status_callback=status_cb)
        if ok:
            r.status = SMSMessageRecipient.Status.SENT
            r.twilio_sid = sid or ''
            r.sent_at = tz.now()
            r.error_code = ''
            r.error_message = ''
        else:
            r.status = SMSMessageRecipient.Status.FAILED
            r.error_code = err_code or ''
            r.error_message = 'send failed'
            # Twilio rejected because the number is opted out (21610). The inbound
            # OptOutType webhook can miss opt-outs, so learn from the block itself and
            # suppress the number globally — no campaign ever re-attempts it.
            if err_code in TWILIO_OPT_OUT_ERROR_CODES:
                PhoneSuppression.objects.get_or_create(
                    phone=r.phone, organization=None,
                    defaults={'reason': PhoneSuppression.Reason.TWILIO_STOP},
                )
        r.save(update_fields=update_fields)


@shared_task
def finalize_sms_campaign_task(results, campaign_id):
    """Chord callback: mark the campaign sent once no recipients remain queued."""
    _finalize_sms_campaign(campaign_id)


def _finalize_sms_campaign(campaign_id):
    from django.utils import timezone as tz
    from tickets.models import SMSCampaign, SMSMessageRecipient

    campaign = SMSCampaign.objects.filter(id=campaign_id).first()
    if not campaign or campaign.status == SMSCampaign.Status.CANCELED:
        return
    if SMSMessageRecipient.objects.filter(
        campaign=campaign, status=SMSMessageRecipient.Status.QUEUED, twilio_sid=''
    ).exists():
        # Still genuinely unsent work outstanding (e.g. a chunk errored); leave
        # 'sending' for the cron recovery pass to re-dispatch. A row that already
        # carries a twilio_sid is done — don't block finalize on it, or a stale
        # callback that regressed its status would hang the campaign forever.
        return
    SMSCampaign.objects.filter(id=campaign.id).update(
        status=SMSCampaign.Status.SENT, sent_at=tz.now()
    )


# Regenerate at most once per ~day: skip an event whose summary was (re)generated
# within this window, so a manual generation earlier today isn't immediately redone.
_SUMMARY_REGEN_MIN_INTERVAL = timedelta(hours=20)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def regenerate_event_summary_task(self, event_id):
    """Regenerate an event's AI summary if its underlying data changed.

    Runs from the daily scan. Only touches events that already have a summary
    (regeneration, not first-time creation) and only after the event has ended.
    Uses a fingerprint of the summary's input data to skip regeneration when
    nothing changed, so unchanged events cost no LLM tokens. On the first run
    for an event (no stored fingerprint), it backfills the fingerprint without
    regenerating, establishing a baseline for future change detection.
    """
    from django.utils import timezone as tz

    from tickets.models import Event
    from tickets.services.event_summary import EventSummaryService
    from tickets.views import _compute_event_stats

    event = (
        Event.objects.filter(id=event_id, deleted_at__isnull=True)
        .select_related('organization', 'venue')
        .first()
    )
    if event is None:
        return 'missing'

    org = event.organization
    # Respect both the display toggle and the auto-regenerate opt-out.
    if not org.ai_event_summary_enabled or not org.ai_event_summary_auto_regenerate:
        return 'disabled'

    # Only regenerate summaries that already exist — never auto-create a first one.
    if not event.ai_summary:
        return 'no-summary'

    # Only after the event has actually ended (timezone-aware end).
    if event.end_datetime() > tz.now():
        return 'not-ended'

    # Enforce "at most once a day": if it was regenerated very recently, wait.
    if event.ai_summary_generated_at and (
        tz.now() - event.ai_summary_generated_at
    ) < _SUMMARY_REGEN_MIN_INTERVAL:
        return 'recent'

    try:
        event_data = _compute_event_stats(event)
        service = EventSummaryService(org)
        fingerprint = service.input_fingerprint(event, event_data)

        # First pass for a pre-existing summary: record the baseline fingerprint
        # without spending tokens. Future changes are then detectable.
        if not event.ai_summary_input_hash:
            event.ai_summary_input_hash = fingerprint
            event.save(update_fields=['ai_summary_input_hash'])
            return 'backfilled'

        # No change since last generation — nothing to do.
        if fingerprint == event.ai_summary_input_hash:
            return 'unchanged'

        result = service.generate_summary(event, event_data)
    except Exception as exc:
        logger.exception("Event summary regeneration failed for event %s", event_id)
        raise self.retry(exc=exc)

    if result is None:
        # Generation was attempted but produced nothing (e.g. missing API key).
        # Don't retry — the fingerprint stays stale so a later run tries again.
        logger.warning("Event summary regeneration produced no text for event %s", event_id)
        return 'failed'

    logger.info("Regenerated AI summary for event %s", event_id)
    return 'regenerated'


# Max chars of the receiver's response body we persist. Kept small: it's only
# for debugging, and we never want to store large or sensitive internal responses.
WEBHOOK_RESPONSE_BODY_LIMIT = 500


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def deliver_webhook_task(self, endpoint_id, event_type, delivery_id, payload):
    """Deliver a signed webhook POST to one endpoint, logging every attempt.

    Enqueued by `tickets.services.webhooks.dispatch.dispatch`. Writes one
    WebhookDelivery row per attempt (retries produce new rows with an incremented
    `attempt`, all sharing `delivery_id`). Retries on connection errors, timeouts,
    and 5xx responses; a 4xx is treated as a permanent (terminal) failure and is
    NOT retried.
    """
    import requests
    from django.conf import settings
    from django.utils import timezone

    from tickets.models import WebhookEndpoint, WebhookDelivery
    from tickets.services.webhooks.signing import build_signed_request
    from tickets.services.webhooks.validation import is_webhook_url_allowed

    try:
        endpoint = WebhookEndpoint.objects.select_related('organization').get(id=endpoint_id)
    except WebhookEndpoint.DoesNotExist:
        logger.warning("WebhookEndpoint %s not found, skipping delivery", endpoint_id)
        return
    if not endpoint.is_active:
        return

    delivery = WebhookDelivery(
        endpoint=endpoint,
        organization=endpoint.organization,
        event_type=event_type,
        delivery_id=delivery_id,
        payload=payload,
        attempt=self.request.retries + 1,
    )

    # SSRF guard at send time (defense in depth vs DNS rebinding / rows created
    # before validation existed). A blocked URL is terminal — never retried.
    if not is_webhook_url_allowed(endpoint.url):
        delivery.success = False
        delivery.error_message = 'Blocked: URL resolves to a private/reserved address or non-https scheme.'
        delivery.save()
        logger.warning("Webhook delivery blocked (SSRF guard) endpoint=%s url=%s", endpoint_id, endpoint.url)
        return

    timeout = getattr(settings, 'WEBHOOK_DELIVERY_TIMEOUT', 10)
    body, headers = build_signed_request(endpoint.secret, event_type, delivery_id, payload)

    try:
        resp = requests.post(endpoint.url, data=body, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        # No response (connection error / timeout) — transient, retry.
        delivery.success = False
        delivery.error_message = str(exc)[:1000]
        delivery.save()
        logger.exception("Webhook delivery failed (no response) endpoint=%s (%s)", endpoint_id, event_type)
        raise self.retry(exc=exc)

    delivery.response_status = resp.status_code
    delivery.response_body = (resp.text or '')[:WEBHOOK_RESPONSE_BODY_LIMIT]
    delivery.success = 200 <= resp.status_code < 300
    if not delivery.success:
        delivery.error_message = f"HTTP {resp.status_code}"[:1000]
    delivery.save()

    endpoint.last_used_at = timezone.now()
    endpoint.save(update_fields=['last_used_at'])

    if delivery.success:
        return
    if 400 <= resp.status_code < 500:
        # Client error — the request is permanently wrong. Don't retry.
        logger.warning(
            "Webhook delivery got terminal %s endpoint=%s (%s), not retrying",
            resp.status_code, endpoint_id, event_type,
        )
        return
    # 5xx (or anything else non-2xx) — transient, retry.
    logger.warning(
        "Webhook delivery got %s endpoint=%s (%s), retrying",
        resp.status_code, endpoint_id, event_type,
    )
    raise self.retry(exc=requests.RequestException(f"HTTP {resp.status_code}"))
