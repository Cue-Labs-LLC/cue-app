"""Manually request the `card_payments` capability on a Stripe Connect
account. Useful for unsticking merchants whose Express account was
created before the capability was requested at create-time.
"""
import logging

from django.conf import settings as django_settings
from django.core.cache import cache as django_cache
from django.core.management.base import BaseCommand, CommandError

from tickets.models import Organization
from tickets.views import _read_stripe_capability, _request_card_payments_capability

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Request the `card_payments` capability on a Stripe Connect '
        'account. Idempotent — re-requesting an active capability is a '
        'no-op for Stripe. Specify exactly one of --org or --account.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--org', dest='org_slug', default=None,
            help="Organization slug. Uses the org's stripe_account_id.",
        )
        parser.add_argument(
            '--account', dest='account_id', default=None,
            help='Stripe Connect account ID (acct_xxx). No DB lookup '
                 'required, but the per-org status cache is still busted '
                 'when a matching org is found.',
        )

    def handle(self, *args, **options):
        import stripe as stripe_lib

        org_slug = options['org_slug']
        account_id = (options['account_id'] or '').strip()
        if bool(org_slug) == bool(account_id):
            raise CommandError('Specify exactly one of --org or --account.')

        org = None
        if org_slug:
            try:
                org = Organization.objects.get(slug=org_slug)
            except Organization.DoesNotExist:
                raise CommandError(f'Organization "{org_slug}" not found.')
            if not org.stripe_account_id:
                raise CommandError(
                    f'Organization "{org_slug}" has no stripe_account_id.'
                )
            account_id = org.stripe_account_id
        else:
            org = Organization.objects.filter(stripe_account_id=account_id).first()

        stripe_lib.api_key = django_settings.STRIPE_SECRET_KEY

        try:
            before = stripe_lib.Account.retrieve(account_id)
        except stripe_lib.error.StripeError as exc:
            raise CommandError(f'Could not retrieve Stripe account: {exc}')

        before_state = _read_stripe_capability(
            getattr(before, 'capabilities', None), 'card_payments',
        ) or 'unrequested'
        country = (getattr(before, 'country', '') or '').upper()
        self.stdout.write(
            f'Before: account={account_id} country={country} '
            f'card_payments={before_state}'
        )

        refreshed = _request_card_payments_capability(stripe_lib, account_id)
        if refreshed is None:
            raise CommandError(
                'Stripe rejected the capability request — check logs for details.'
            )

        after_state = _read_stripe_capability(
            getattr(refreshed, 'capabilities', None), 'card_payments',
        ) or 'unrequested'
        currently_due = list(
            getattr(getattr(refreshed, 'requirements', None), 'currently_due', None) or []
        )
        self.stdout.write(f'After:  card_payments={after_state}')
        if currently_due:
            self.stdout.write(self.style.WARNING(
                f'  Stripe wants more info before activating: {currently_due}'
            ))

        if org is not None:
            django_cache.delete(f'tap_to_pay_status:{org.pk}')
            self.stdout.write(f'Cleared status cache for org={org.slug}.')

        self.stdout.write(self.style.SUCCESS(
            'Done. Stripe usually activates the capability within a few '
            'seconds in test mode. Pull-to-refresh in the iOS app.'
        ))
