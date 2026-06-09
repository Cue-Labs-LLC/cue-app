"""Management command to backfill refund state from Stripe.

Refunds issued from the Stripe dashboard before the charge.refunded webhook
handler existed never updated the DB — sessions stayed COMPLETED while the
money left the platform balance, drifting the Finance page figures. This
command scans the platform's Stripe refunds, finds the matching
StripeCheckoutSession rows, and applies any missed refund state through the
same idempotent sync the webhook uses (_sync_charge_refund).
"""

import logging
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tickets.models import Organization, StripeCheckoutSession

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Backfill refund state from Stripe for sessions that missed '
        'charge.refunded webhooks (e.g. dashboard refunds issued before the '
        'handler existed). Dry-run by default; use --apply to write changes.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--org',
            dest='org_slug',
            default=None,
            help='Limit backfill to a specific organization slug (default: all orgs).',
        )
        parser.add_argument(
            '--days',
            type=int,
            default=None,
            help='Only scan Stripe refunds created in the last N days (default: all time).',
        )
        parser.add_argument(
            '--apply',
            action='store_true',
            default=False,
            help='Write refund state to the database (default: dry-run).',
        )

    def handle(self, *args, **options):
        import stripe as stripe_lib
        from django.conf import settings as django_settings

        from tickets.views import (
            _find_session_for_payment_intent, _stripe_value, _sync_charge_refund,
        )

        stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY

        org = None
        if options['org_slug']:
            try:
                org = Organization.objects.get(slug=options['org_slug'])
            except Organization.DoesNotExist:
                raise CommandError(f'Organization "{options["org_slug"]}" not found.')

        apply = options['apply']
        if not apply:
            self.stdout.write(self.style.WARNING(
                'DRY-RUN — no changes will be saved. Re-run with --apply to write.\n'
            ))

        list_kwargs = {'limit': 100}
        if options['days']:
            since = timezone.now() - timedelta(days=options['days'])
            list_kwargs['created'] = {'gte': int(since.timestamp())}

        # One refund feed pass; a charge with several partial refunds appears
        # once. The Charge object is the source of truth for the CUMULATIVE
        # amount_refunded, so retrieve it per unique charge.
        charge_ids = []
        seen = set()
        try:
            for refund in stripe_lib.Refund.list(**list_kwargs).auto_paging_iter():
                charge_id = _stripe_value(refund, 'charge')
                if charge_id and charge_id not in seen:
                    seen.add(charge_id)
                    charge_ids.append(charge_id)
        except stripe_lib.error.StripeError as e:
            raise CommandError(f'Could not list Stripe refunds: {e}')

        if not charge_ids:
            self.stdout.write('No Stripe refunds found in the scan window.')
            return

        self.stdout.write(f'Found {len(charge_ids)} refunded charge(s) to check.\n')

        updated = 0
        already_current = 0
        not_ours = 0
        skipped = 0
        errors = 0

        for charge_id in charge_ids:
            try:
                charge = stripe_lib.Charge.retrieve(charge_id)
            except stripe_lib.error.StripeError as e:
                self.stdout.write(self.style.ERROR(f'  ERROR {charge_id} — Stripe error: {e}'))
                errors += 1
                continue

            pi_id = _stripe_value(charge, 'payment_intent')
            if not pi_id:
                self.stdout.write(f'  IGNORE {charge_id} — charge has no payment_intent')
                not_ours += 1
                continue

            # Matches PI-flow rows directly and legacy Checkout-flow rows
            # (cs_… id, blank stripe_payment_intent_id) via a Stripe lookup.
            session = _find_session_for_payment_intent(pi_id)
            if session is None:
                # SMS top-up or charge that isn't ours.
                amount = Decimal(int(_stripe_value(charge, 'amount_refunded') or 0)) / 100
                self.stdout.write(
                    f'  IGNORE {charge_id} — no matching session '
                    f'(pi={pi_id}, refunded ${amount}, '
                    f'desc={_stripe_value(charge, "description")!r})'
                )
                not_ours += 1
                continue

            label = (
                f'{charge_id} (pi={pi_id}, org={session.organization.slug}, '
                f'session status={session.status})'
            )

            if org and session.organization_id != org.id:
                skipped += 1
                continue

            if session.status not in (
                StripeCheckoutSession.Status.COMPLETED,
                StripeCheckoutSession.Status.PARTIALLY_REFUNDED,
                StripeCheckoutSession.Status.REFUNDED,
            ):
                self.stdout.write(f'  SKIP  {label} — session never completed')
                skipped += 1
                continue

            # Mirror _sync_charge_refund's no-op guard to report accurately.
            order = session.ticket_order
            refunded_total = Decimal(int(_stripe_value(charge, 'amount_refunded') or 0)) / 100
            fully_refunded = bool(_stripe_value(charge, 'refunded'))
            target_status = (
                StripeCheckoutSession.Status.REFUNDED if fully_refunded
                else StripeCheckoutSession.Status.PARTIALLY_REFUNDED
            )
            if session.status == target_status and (
                order is None or order.refunded_amount >= refunded_total
            ):
                self.stdout.write(f'  OK    {label} — already current')
                already_current += 1
                continue

            self.stdout.write(
                f'  {"UPDATE" if apply else "WOULD UPDATE"}  {label} → '
                f'{target_status} (refunded ${refunded_total})'
            )
            if apply:
                _sync_charge_refund(charge)
            updated += 1

        self.stdout.write('')
        summary = (
            f'Updated: {updated}  Already current: {already_current}  '
            f'Not ours: {not_ours}  Skipped: {skipped}  Errors: {errors}'
        )
        if apply:
            self.stdout.write(self.style.SUCCESS(f'Done. {summary}'))
        else:
            self.stdout.write(self.style.WARNING(f'DRY-RUN complete. Would update: {updated}  '
                                                 f'Already current: {already_current}  '
                                                 f'Not ours: {not_ours}  Skipped: {skipped}  Errors: {errors}'))
            if updated:
                self.stdout.write('Re-run with --apply to save changes.')
