"""
One-time (re-runnable) true-up: move settled, platform-held legacy organizer
balances into each org's Stripe connected account.

Context: before the destination-charge cutover, online ticket money landed on
the platform Stripe account and only reached the organizer's Express account
at payout time. After cutover, the connected balance is the source of truth
for "Ready to Withdraw" — this command transfers what the platform still
holds (settled legacy sessions minus platform-pool payouts) so the Finance
page and Stripe agree.

Crash-safe ordering (never double-pays):

    lock org row ──► compute legacy balance ──► COMMIT a PENDING
    Payout(origin=migration) row              (own transaction)
            │
            ▼
    Transfer.create(idempotency_key=f'trueup-{payout.id}')   <- stable key
            │
            ▼
    save stripe_transfer_id, mark COMPLETED

A crash between the Stripe call and the final save leaves a PENDING row that
self-deducts from the balance math (so re-runs transfer $0) and is completed
by --repair, which replays the same idempotency key — Stripe returns the
original Transfer instead of creating a second one.

Re-runnable by construction: each run transfers max(0, legacy_settled -
platform_pool_payouts) and records exactly that as a platform-pool payout, so
the next run computes 0. Legacy sessions that settle later transfer their
delta on a later run. Run weekly until dry-run reports $0 everywhere.

Prerequisites (per the cutover checklist): run backfill_refund_state and
sync_payout_statuses first so refund drift and stuck payouts don't feed wrong
amounts into the wire.

Usage:
    python manage.py migrate_legacy_balances             # dry-run, all orgs
    python manage.py migrate_legacy_balances --org SLUG  # dry-run, one org
    python manage.py migrate_legacy_balances --apply     # execute transfers
    python manage.py migrate_legacy_balances --repair    # complete stranded PENDING rows
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from tickets.models import Organization, Payout


class Command(BaseCommand):
    help = 'Transfer settled platform-held legacy balances to organizer connected accounts.'

    def add_arguments(self, parser):
        parser.add_argument('--org', help='Limit to one organization (slug).')
        parser.add_argument('--apply', action='store_true', help='Execute transfers (default: dry-run).')
        parser.add_argument(
            '--repair', action='store_true',
            help='Complete stranded PENDING migration rows by replaying their idempotency key.',
        )

    def handle(self, *args, **options):
        import stripe as stripe_lib
        from django.conf import settings as django_settings
        from tickets.views import (
            _compute_legacy_settled_balance,
            _get_stripe_platform_available_cents,
            _bust_connected_balance_cache,
        )

        stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY
        apply_mode = options['apply']
        repair_mode = options['repair']

        orgs = Organization.objects.filter(
            stripe_onboarding_complete=True,
        ).exclude(stripe_account_id='')
        if options['org']:
            orgs = orgs.filter(slug=options['org'])
            if not orgs.exists():
                self.stdout.write(self.style.ERROR(f"No onboarded org with slug {options['org']!r}"))
                return

        if not apply_mode and not repair_mode:
            self.stdout.write(self.style.WARNING('DRY-RUN — no transfers will be created. Use --apply to execute.'))

        if repair_mode:
            self._repair(stripe_lib, orgs)
            return

        total_moved = Decimal('0.00')
        for org in orgs.order_by('name'):
            stranded = Payout.objects.filter(
                organization=org,
                origin=Payout.Origin.MIGRATION,
                status=Payout.Status.PENDING,
            )
            if stranded.exists():
                self.stdout.write(self.style.ERROR(
                    f'{org.name}: {stranded.count()} stranded PENDING migration row(s) — run --repair first.'
                ))
                continue

            raw = _compute_legacy_settled_balance(org, clamp=False)
            amount = max(Decimal('0.00'), raw)
            if raw < 0:
                self.stdout.write(self.style.WARNING(
                    f'{org.name}: raw legacy balance is {raw} (refund after true-up — platform absorbed). Skipping.'
                ))
                continue
            if amount < Decimal('0.01'):
                self.stdout.write(f'{org.name}: $0.00 — nothing to move.')
                continue

            self.stdout.write(f'{org.name}: ${amount} settled legacy balance to move.')
            if not apply_mode:
                total_moved += amount
                continue

            platform_cents = _get_stripe_platform_available_cents(use_cache=False)
            if platform_cents is None or Decimal(str(platform_cents)) / 100 < amount:
                self.stdout.write(self.style.ERROR(
                    f'{org.name}: platform available balance '
                    f'({platform_cents if platform_cents is None else Decimal(str(platform_cents)) / 100}) '
                    f'does not cover ${amount}. Skipping.'
                ))
                continue

            # Commit the PENDING row before calling Stripe: its id is the
            # stable idempotency anchor, and as a non-failed platform-pool
            # payout it self-deducts from the balance math, so a crash after
            # the Stripe call can only strand a repairable row — never
            # double-pay. The org-row lock serializes concurrent runs, and the
            # balance is recomputed under the lock so a payout committed by a
            # concurrent run between our first compute and the lock deducts.
            with transaction.atomic():
                Organization.objects.select_for_update().get(pk=org.pk)
                if Payout.objects.filter(
                    organization=org,
                    origin=Payout.Origin.MIGRATION,
                    status=Payout.Status.PENDING,
                ).exists():
                    self.stdout.write(self.style.ERROR(f'{org.name}: concurrent run detected. Skipping.'))
                    continue
                amount = _compute_legacy_settled_balance(org)
                if amount < Decimal('0.01'):
                    self.stdout.write(f'{org.name}: balance already moved by a concurrent run.')
                    continue
                payout = Payout.objects.create(
                    organization=org,
                    amount=amount,
                    status=Payout.Status.PENDING,
                    origin=Payout.Origin.MIGRATION,
                    initiated_by=None,
                    notes='Balance migration to Stripe account',
                )

            if self._execute_transfer(stripe_lib, django_settings, org, payout):
                total_moved += amount
            _bust_connected_balance_cache(org)

        self.stdout.write(self.style.SUCCESS(
            f'{"Moved" if apply_mode else "Would move"} ${total_moved} total.'
        ))

    def _execute_transfer(self, stripe_lib, django_settings, org, payout):
        try:
            transfer = stripe_lib.Transfer.create(
                amount=int(payout.amount * 100),
                currency=django_settings.STRIPE_CURRENCY,
                destination=org.stripe_account_id,
                description=f'Balance migration to {org.name}',
                metadata={
                    'org_id': str(org.id),
                    'payout_id': str(payout.id),
                    'reason': 'legacy_balance_migration',
                },
                idempotency_key=f'trueup-{payout.id}',
            )
        except stripe_lib.error.StripeError as e:
            payout.status = Payout.Status.FAILED
            payout.notes = (payout.notes + f' [Stripe error: {str(e)[:400]}]')[:500]
            payout.save(update_fields=['status', 'notes'])
            self.stdout.write(self.style.ERROR(f'{org.name}: transfer failed — {e}'))
            return False

        payout.stripe_transfer_id = transfer.id
        payout.status = Payout.Status.COMPLETED
        payout.save(update_fields=['stripe_transfer_id', 'status'])
        self.stdout.write(self.style.SUCCESS(f'{org.name}: transferred ${payout.amount} ({transfer.id}).'))
        return True

    def _repair(self, stripe_lib, orgs):
        from django.conf import settings as django_settings
        from tickets.views import _bust_connected_balance_cache

        stranded = Payout.objects.filter(
            organization__in=orgs,
            origin=Payout.Origin.MIGRATION,
            status=Payout.Status.PENDING,
        ).select_related('organization').order_by('created_at')
        if not stranded:
            self.stdout.write('No stranded PENDING migration rows.')
            return
        for payout in stranded:
            org = payout.organization
            self.stdout.write(f'{org.name}: replaying trueup-{payout.id} (${payout.amount})…')
            if self._execute_transfer(stripe_lib, django_settings, org, payout):
                _bust_connected_balance_cache(org)
