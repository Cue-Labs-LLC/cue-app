"""Reconcile the PhoneSuppression list against Twilio's opt-out blocks.

Twilio is the source of truth for opt-outs: when someone replies STOP, Twilio's
Advanced Opt-Out list blocks every subsequent send with Error 21610, which dings the
account's Compliance score. Our PhoneSuppression table is only a *mirror*, populated
by the inbound OptOutType webhook — so it misses opt-outs the webhook never saw
(legacy STOPs from before suppression tracking existed, dropped callbacks, or STOPs to
a different sender). Those numbers stay in campaign audiences and get re-blocked.

This command scans Twilio's outbound message logs for opt-out blocks (Error 21610) and
mirrors each blocked number into PhoneSuppression as a global suppression, closing the
gap. Dry-run by default; use --apply to write changes.

    python manage.py reconcile_sms_opt_outs --days 180 --apply
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tickets.models import PhoneSuppression
from tickets.sms import normalize_phone, TWILIO_OPT_OUT_ERROR_CODES

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Mirror Twilio's opt-out blocks (Error 21610) into PhoneSuppression so "
        "opted-out numbers are never re-attempted. Dry-run by default; use --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=90,
            help='How many days of Twilio message history to scan (default: 90).',
        )
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Write suppressions. Without this flag the command only reports.',
        )

    def handle(self, *args, **options):
        days = options['days']
        apply = options['apply']

        account_sid = getattr(settings, 'TWILIO_ACCOUNT_SID', '')
        auth_token = getattr(settings, 'TWILIO_AUTH_TOKEN', '')
        if not account_sid or not auth_token:
            raise CommandError(
                'TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are not configured.'
            )

        try:
            from twilio.rest import Client
        except ImportError:
            raise CommandError('The twilio package is not installed.')

        client = Client(account_sid, auth_token)
        since = timezone.now() - timedelta(days=days)

        self.stdout.write(
            f"Scanning Twilio messages since {since:%Y-%m-%d} for opt-out blocks "
            f"({', '.join(sorted(TWILIO_OPT_OUT_ERROR_CODES))})..."
        )

        # Twilio's SDK paginates transparently; date_sent_after narrows the scan.
        blocked_phones = set()
        scanned = 0
        for msg in client.messages.list(date_sent_after=since):
            scanned += 1
            if msg.error_code is None:
                continue
            if str(msg.error_code) not in TWILIO_OPT_OUT_ERROR_CODES:
                continue
            phone = normalize_phone(msg.to or '')
            if phone:
                blocked_phones.add(phone)

        self.stdout.write(
            f"Scanned {scanned} messages; found {len(blocked_phones)} opted-out numbers."
        )

        # Skip numbers already globally suppressed so counts reflect real gap-closing.
        existing = set(
            PhoneSuppression.objects.filter(
                organization__isnull=True, phone__in=blocked_phones
            ).values_list('phone', flat=True)
        )
        missing = sorted(blocked_phones - existing)

        if not missing:
            self.stdout.write(self.style.SUCCESS(
                'PhoneSuppression already covers every opted-out number. Nothing to do.'
            ))
            return

        if not apply:
            self.stdout.write(self.style.WARNING(
                f'DRY RUN: {len(missing)} number(s) would be suppressed. '
                f'Re-run with --apply to write. Sample: {missing[:10]}'
            ))
            return

        created = 0
        for phone in missing:
            _, was_created = PhoneSuppression.objects.get_or_create(
                phone=phone, organization=None,
                defaults={'reason': PhoneSuppression.Reason.TWILIO_STOP},
            )
            created += int(was_created)

        self.stdout.write(self.style.SUCCESS(
            f'Suppressed {created} previously-missed opted-out number(s).'
        ))
