"""Management command to sync open payout statuses from Stripe."""

import logging

from django.core.management.base import BaseCommand, CommandError

from tickets.models import Organization, Payout

logger = logging.getLogger(__name__)

STATUS_MAP = {
    'in_transit': Payout.Status.IN_TRANSIT,
    'paid':       Payout.Status.COMPLETED,
    'failed':     Payout.Status.FAILED,
    'canceled':   Payout.Status.FAILED,
}


class Command(BaseCommand):
    help = (
        'Sync open payout statuses from Stripe. Useful when payout.paid webhooks '
        'were missed and records are stuck in PENDING or IN_TRANSIT. '
        'Dry-run by default; use --apply to write changes.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--org',
            dest='org_slug',
            default=None,
            help='Limit sync to a specific organization slug (default: all orgs).',
        )
        parser.add_argument(
            '--apply',
            action='store_true',
            default=False,
            help='Write status changes to the database (default: dry-run).',
        )

    def handle(self, *args, **options):
        import stripe as stripe_lib

        org_slug = options['org_slug']
        apply = options['apply']

        qs = Payout.objects.filter(
            status__in=[Payout.Status.PENDING, Payout.Status.IN_TRANSIT],
        ).select_related('organization').order_by('created_at')

        if org_slug:
            try:
                org = Organization.objects.get(slug=org_slug)
            except Organization.DoesNotExist:
                raise CommandError(f'Organization "{org_slug}" not found.')
            qs = qs.filter(organization=org)

        payouts = list(qs)

        if not payouts:
            self.stdout.write('No open payouts found.')
            return

        self.stdout.write(f'Found {len(payouts)} open payout(s) to check.\n')

        if not apply:
            self.stdout.write(self.style.WARNING('DRY-RUN — no changes will be saved. Re-run with --apply to write.\n'))

        updated = 0
        already_current = 0
        skipped = 0
        errors = 0

        for payout in payouts:
            org = payout.organization
            label = f'Payout {payout.id} (${payout.amount}, org={org.slug}, status={payout.status})'

            if not org.stripe_account_id:
                self.stdout.write(f'  SKIP  {label} — org has no Stripe account')
                skipped += 1
                continue

            # --- Path A: payout has a stripe_payout_id — retrieve directly ---
            if payout.stripe_payout_id:
                try:
                    stripe_payout = stripe_lib.Payout.retrieve(
                        payout.stripe_payout_id,
                        stripe_account=org.stripe_account_id,
                    )
                except stripe_lib.error.StripeError as e:
                    self.stdout.write(self.style.ERROR(f'  ERROR {label} — Stripe error: {e}'))
                    errors += 1
                    continue

                stripe_status = stripe_payout.get('status') if isinstance(stripe_payout, dict) else getattr(stripe_payout, 'status', None)
                new_status = STATUS_MAP.get(stripe_status)

                if not new_status or payout.status == new_status:
                    self.stdout.write(f'  OK    {label} — Stripe status={stripe_status!r}, no change needed')
                    already_current += 1
                    continue

                self.stdout.write(
                    f'  {"UPDATE" if apply else "WOULD UPDATE"}  {label} → {new_status} (Stripe={stripe_status!r})'
                )
                if apply:
                    payout.status = new_status
                    payout.save(update_fields=['status'])
                updated += 1
                continue

            # --- Path B: payout has stripe_transfer_id but no stripe_payout_id ---
            # LEGACY ONLY: pre-cutover rows from the Transfer+Payout flow.
            # Post-cutover payouts always store the po_ id at create time (or
            # arrive via webhook with one), so the amount-match below never
            # applies to them. Self-extinguishing once legacy rows drain.
            if payout.stripe_transfer_id:
                try:
                    transfer = stripe_lib.Transfer.retrieve(
                        payout.stripe_transfer_id,
                        expand=['reversals'],
                    )
                except stripe_lib.error.StripeError as e:
                    self.stdout.write(self.style.ERROR(f'  ERROR {label} — Transfer retrieve error: {e}'))
                    errors += 1
                    continue

                # List payouts on the connected account for the same amount to find the match
                amount_cents = int(payout.amount * 100)
                try:
                    stripe_payouts = stripe_lib.Payout.list(
                        limit=10,
                        stripe_account=org.stripe_account_id,
                    )
                except stripe_lib.error.StripeError as e:
                    self.stdout.write(self.style.ERROR(f'  ERROR {label} — Payout list error: {e}'))
                    errors += 1
                    continue

                items = stripe_payouts.get('data', []) if isinstance(stripe_payouts, dict) else getattr(stripe_payouts, 'data', [])
                match = next(
                    (p for p in items if (p.get('amount') if isinstance(p, dict) else getattr(p, 'amount', None)) == amount_cents),
                    None,
                )

                if not match:
                    self.stdout.write(f'  SKIP  {label} — has transfer_id but could not find matching Stripe payout')
                    skipped += 1
                    continue

                matched_payout_id = match.get('id') if isinstance(match, dict) else getattr(match, 'id', None)
                stripe_status = match.get('status') if isinstance(match, dict) else getattr(match, 'status', None)
                new_status = STATUS_MAP.get(stripe_status)

                update_fields = []
                if not payout.stripe_payout_id and matched_payout_id:
                    payout.stripe_payout_id = matched_payout_id
                    update_fields.append('stripe_payout_id')
                if new_status and payout.status != new_status:
                    payout.status = new_status
                    update_fields.append('status')

                if not update_fields:
                    self.stdout.write(f'  OK    {label} — no change needed')
                    already_current += 1
                    continue

                self.stdout.write(
                    f'  {"UPDATE" if apply else "WOULD UPDATE"}  {label} — '
                    f'stripe_payout_id={matched_payout_id}, status → {new_status} (Stripe={stripe_status!r})'
                )
                if apply:
                    payout.save(update_fields=update_fields)
                updated += 1
                continue

            self.stdout.write(f'  SKIP  {label} — no stripe_payout_id or stripe_transfer_id, cannot look up')
            skipped += 1

        self.stdout.write('')
        if apply:
            self.stdout.write(self.style.SUCCESS(
                f'Done. Updated: {updated}  Already current: {already_current}  Skipped: {skipped}  Errors: {errors}'
            ))
        else:
            self.stdout.write(self.style.WARNING(
                f'DRY-RUN complete. Would update: {updated}  Already current: {already_current}  Skipped: {skipped}  Errors: {errors}'
            ))
            if updated:
                self.stdout.write('Re-run with --apply to save changes.')
