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

    from decimal import Decimal
    try:
        service_fee = Decimal(order.stripe_checkout_session.platform_fee_cents) / 100
    except Exception:
        service_fee = Decimal('0.00')

    context = {
        'order': order,
        'customer': customer,
        'event': order.event,
        'tickets': list(order.tickets.all()),
        'show_qr_code': bool(qr_png_bytes),
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

def _with_stop_footer(body):
    """Append the required opt-out footer unless the body already mentions STOP."""
    if 'STOP' in (body or '').upper():
        return body
    return f"{body}\n\nReply STOP to opt out"


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
        'organization', 'recipient_list'
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
        recipients = campaign.recipient_list.materialize(org, cap=cap)
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

    queued_ids = [
        str(i) for i in SMSMessageRecipient.objects.filter(
            campaign=campaign, status=SMSMessageRecipient.Status.QUEUED
        ).values_list('id', flat=True)
    ]
    if not queued_ids:
        _finalize_sms_campaign(campaign.id)
        return

    chunk_size = 100
    header = [
        send_sms_chunk_task.s(str(campaign.id), queued_ids[i:i + chunk_size])
        for i in range(0, len(queued_ids), chunk_size)
    ]
    chord(header)(finalize_sms_campaign_task.s(str(campaign.id)))


@shared_task(bind=True, max_retries=2, default_retry_delay=30, rate_limit='3/s')
def send_sms_chunk_task(self, campaign_id, recipient_ids):
    """Send one chunk of a campaign. Per-recipient failure is isolated — one bad
    number never aborts the chunk. Global throughput is metered by the Twilio
    Messaging Service queue plus this task's rate_limit."""
    import secrets
    from django.conf import settings
    from django.urls import reverse, NoReverseMatch
    from django.utils import timezone as tz
    from tickets.models import SMSCampaign, SMSMessageRecipient
    from tickets.sms import send_sms

    campaign = SMSCampaign.objects.filter(id=campaign_id).first()
    if not campaign or campaign.status == SMSCampaign.Status.CANCELED:
        return

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
    # public URL to point at. Untracked sends reuse one shared body.
    track_links = bool(campaign.link_url) and is_public
    shared_body = _with_stop_footer(campaign.body)

    for r in SMSMessageRecipient.objects.filter(
        id__in=recipient_ids, status=SMSMessageRecipient.Status.QUEUED
    ):
        body = shared_body
        update_fields = ['status', 'twilio_sid', 'sent_at', 'error_code', 'error_message', 'updated_at']
        if track_links:
            token = secrets.token_urlsafe(8)
            tracked = f"{site_url}{reverse('tickets:sms_click_redirect', kwargs={'token': token})}"
            body = _with_stop_footer(campaign.body.replace(campaign.link_url, tracked, 1))
            r.click_token = token
            update_fields.append('click_token')

        ok, sid = send_sms(r.phone, body, status_callback=status_cb)
        if ok:
            r.status = SMSMessageRecipient.Status.SENT
            r.twilio_sid = sid or ''
            r.sent_at = tz.now()
            r.error_code = ''
            r.error_message = ''
        else:
            r.status = SMSMessageRecipient.Status.FAILED
            r.error_message = 'send failed'
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
        campaign=campaign, status=SMSMessageRecipient.Status.QUEUED
    ).exists():
        # Still work outstanding (e.g. a chunk errored); leave 'sending' for the
        # cron recovery pass to re-dispatch.
        return
    SMSCampaign.objects.filter(id=campaign.id).update(
        status=SMSCampaign.Status.SENT, sent_at=tz.now()
    )
