"""Backfill an SMS-credit top-up from a paid Stripe Checkout Session.

Use when a `checkout.session.completed` event failed to fulfill (e.g. the webhook
500'd) so the org was charged by Stripe but their prepaid SMS wallet was never
credited. Crediting is idempotent — keyed on the Checkout Session id — so running
this on an already-credited session is a safe no-op.

    python manage.py backfill_sms_credit_session cs_test_a1GAPTaV…
"""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Credit an org SMS wallet from a paid Stripe Checkout Session id. '
        'Idempotent (keyed on the session id) — safe to re-run. Use after a '
        'missed/failed checkout.session.completed fulfillment.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            'session_id',
            help='Stripe Checkout Session id (cs_…) to fulfill.',
        )

    def handle(self, *args, **options):
        session_id = options['session_id']
        api_key = getattr(settings, 'STRIPE_SECRET_KEY', '')
        if not api_key:
            raise CommandError('STRIPE_SECRET_KEY is not configured in this environment.')

        import stripe as stripe_lib
        from tickets.models import Organization, SMSCreditTransaction
        from tickets.views import _fulfill_sms_credit_checkout

        stripe_lib.api_key = api_key
        try:
            session = stripe_lib.checkout.Session.retrieve(session_id)
        except Exception as exc:
            raise CommandError(f'Could not retrieve Checkout Session {session_id}: {exc}')

        already = SMSCreditTransaction.objects.filter(
            stripe_checkout_session_id=session_id
        ).first()
        if already:
            self.stdout.write(self.style.WARNING(
                f'Session {session_id} already credited '
                f'({already.amount_cents}¢ to org {already.organization_id}) — nothing to do.'
            ))
            return

        # _fulfill_sms_credit_checkout validates kind/payment_status and ignores
        # anything that isn't a paid sms_credits session.
        _fulfill_sms_credit_checkout(session)

        tx = SMSCreditTransaction.objects.filter(
            stripe_checkout_session_id=session_id
        ).first()
        if not tx:
            raise CommandError(
                f'Session {session_id} did not credit any wallet. It may be unpaid, '
                f'not an SMS-credits checkout, or missing metadata.'
            )

        org = Organization.objects.filter(id=tx.organization_id).first()
        org_name = org.name if org else tx.organization_id
        self.stdout.write(self.style.SUCCESS(
            f'Credited {tx.amount_cents}¢ to "{org_name}" '
            f'(new balance {tx.balance_after_cents}¢) from session {session_id}.'
        ))
