"""
Hard-reset an organization's loyalty points back to scratch.

Wraps ``reset_points_for_organization`` (tickets/services/loyalty/points.py):
deletes the org's entire points ledger, zeroes every customer's
``points_balance`` and ``lifetime_points``, and clears
``loyalty_points_backfilled_at`` so a fresh backfill can run cleanly afterward.

This is IRREVERSIBLE — it truncates ledger history rather than appending
offsetting rows — and it is ORG-WIDE: points/balances are (customer,
organization) state, so this clears them across every program the org owns.

Dry-run by default; ``--apply`` executes and prompts for a typed confirmation
(the org slug) unless ``--no-input`` is given. Refuses to run while any of the
org's programs has a recalc/backfill in flight.

Usage:
    python manage.py reset_loyalty_points --org SLUG              # dry-run
    python manage.py reset_loyalty_points --org SLUG --apply      # execute (prompts)
    python manage.py reset_loyalty_points --org SLUG --apply --no-input
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from tickets.models import Customer, LoyaltyPointsTransaction, LoyaltyProgram, Organization
from tickets.services.loyalty import reset_points_for_organization


class Command(BaseCommand):
    help = "Hard-reset an organization's loyalty points (ledger + balances) to zero."

    def add_arguments(self, parser):
        parser.add_argument('--org', required=True, help='Organization slug to reset.')
        parser.add_argument('--apply', action='store_true', help='Execute the reset (default: dry-run).')
        parser.add_argument(
            '--no-input', action='store_true', dest='no_input',
            help='Skip the typed confirmation prompt (for scripted/non-interactive runs).',
        )

    def handle(self, *args, **options):
        try:
            org = Organization.objects.get(slug=options['org'])
        except Organization.DoesNotExist:
            raise CommandError(f"No organization with slug {options['org']!r}.")

        # Refuse while a backfill/recalc is mutating balances — reset truncates
        # the ledger, so an in-flight job would race against the wipe.
        if LoyaltyProgram.objects.filter(
            organization=org, recalc_in_progress=True, deleted_at__isnull=True,
        ).exists():
            raise CommandError(
                f'{org.name}: a recalc/backfill is in progress. Wait for it to finish, then retry.'
            )

        ledger_rows = LoyaltyPointsTransaction.objects.filter(organization=org).count()
        customers_with_points = (
            Customer.objects.filter(organization=org)
            .exclude(Q(points_balance=0) & Q(lifetime_points=0))
            .count()
        )

        self.stdout.write(
            f'{org.name} ({org.slug}): {ledger_rows} ledger row(s), '
            f'{customers_with_points} customer(s) with non-zero points.'
        )

        if ledger_rows == 0 and customers_with_points == 0:
            self.stdout.write(self.style.SUCCESS('Nothing to reset — points are already at zero.'))
            return

        if not options['apply']:
            self.stdout.write(self.style.WARNING(
                'DRY-RUN — no changes made. Re-run with --apply to wipe the ledger and zero balances.'
            ))
            return

        if not options['no_input']:
            self.stdout.write(self.style.WARNING(
                'This permanently deletes the points ledger and zeroes all balances. It cannot be undone.'
            ))
            typed = input(f'Type the org slug ({org.slug}) to confirm: ').strip()
            if typed != org.slug:
                raise CommandError('Confirmation did not match. Aborted.')

        result = reset_points_for_organization(org)
        self.stdout.write(self.style.SUCCESS(
            f"Reset complete — deleted {result['transactions_deleted']} ledger row(s), "
            f"zeroed {result['customers_reset']} customer(s)."
        ))
