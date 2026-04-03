"""Management command to remove test-mode Stripe data from an organization."""

from django.core.management.base import BaseCommand, CommandError

from tickets.models import Organization, Payout, StripeCheckoutSession


class Command(BaseCommand):
    help = (
        'Delete Payout and StripeCheckoutSession records for an org and clear '
        'the linked Stripe account ID. Use after disconnecting a test-mode '
        'Stripe account before connecting a live one.'
    )

    def add_arguments(self, parser):
        parser.add_argument('org_slug', help='Slug of the organization to clear.')
        parser.add_argument(
            '--apply',
            action='store_true',
            default=False,
            help='Write changes to the database (default: dry-run).',
        )

    def handle(self, *args, **options):
        org_slug = options['org_slug']
        apply = options['apply']

        try:
            org = Organization.objects.get(slug=org_slug)
        except Organization.DoesNotExist:
            raise CommandError(f'Organization "{org_slug}" not found.')

        payout_count = Payout.objects.filter(organization=org).count()
        session_count = StripeCheckoutSession.objects.filter(organization=org).count()

        self.stdout.write(f'Organization : {org.name} ({org.slug})')
        self.stdout.write(f'Stripe account: {org.stripe_account_id or "(none)"}')
        self.stdout.write(f'Payouts       : {payout_count}')
        self.stdout.write(f'Checkout sessions: {session_count}')

        if not apply:
            self.stdout.write(self.style.WARNING(
                '\nDRY-RUN — no changes saved. Re-run with --apply to delete.'
            ))
            return

        Payout.objects.filter(organization=org).delete()
        StripeCheckoutSession.objects.filter(organization=org).delete()

        org.stripe_account_id = ''
        org.stripe_onboarding_complete = False
        org.save(update_fields=['stripe_account_id', 'stripe_onboarding_complete'])

        self.stdout.write(self.style.SUCCESS(
            f'\nDeleted {payout_count} payout(s) and {session_count} checkout session(s). '
            'Stripe account unlinked.'
        ))
