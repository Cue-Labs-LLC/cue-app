"""Backfill PhoneSuppression with hard bounces from past marketing sends.

The live paths (send_sms_chunk_task + twilio_sms_status_webhook, via
tickets.sms.handle_delivery_failure) suppress dead numbers going forward. This command
retroactively scrubs numbers that ALREADY failed before that logic existed, so they drop
out of future audiences instead of being re-texted (and re-charged) blast after blast.

It reads our own SMSMessageRecipient rows — we already store Twilio's error_code per
message, so no Twilio API call is needed:

  - hard-bounce codes (unknown/invalid/landline/not-SMS-capable) → suppress on sight
  - transient codes (handset off/unreachable) → suppress only numbers that failed at
    least SMS_BOUNCE_STRIKE_THRESHOLD *distinct* campaigns

Dry-run by default; use --apply to write changes.

    python manage.py suppress_sms_bounces --apply
"""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Count

from tickets.models import PhoneSuppression, SMSMessageRecipient
from tickets.sms import (
    TWILIO_HARD_BOUNCE_ERROR_CODES,
    TWILIO_TRANSIENT_FAIL_ERROR_CODES,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Suppress numbers that hard-bounced (or repeatedly failed transiently) in past "
        "marketing sends so they leave future audiences. Dry-run by default; use --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Write suppressions. Without this flag the command only reports.',
        )

    def handle(self, *args, **options):
        apply = options['apply']
        threshold = getattr(settings, 'SMS_BOUNCE_STRIKE_THRESHOLD', 3)

        # Hard bounces: any single failure with a permanent-undeliverable code.
        hard = set(
            SMSMessageRecipient.objects
            .filter(error_code__in=TWILIO_HARD_BOUNCE_ERROR_CODES)
            .exclude(phone='')
            .values_list('phone', flat=True)
        )

        # Transient: only numbers that failed the code across enough distinct campaigns.
        transient = set(
            SMSMessageRecipient.objects
            .filter(error_code__in=TWILIO_TRANSIENT_FAIL_ERROR_CODES)
            .exclude(phone='')
            .values('phone')
            .annotate(n=Count('campaign_id', distinct=True))
            .filter(n__gte=threshold)
            .values_list('phone', flat=True)
        )

        # A number that hard-bounced wins the hard classification even if it also has
        # transient strikes; both map to reason=BOUNCE, so just union the phone sets.
        to_suppress = hard | transient
        self.stdout.write(
            f"Found {len(hard)} hard-bounced and {len(transient)} repeatedly-transient "
            f"number(s) (strike threshold {threshold}); {len(to_suppress)} unique."
        )

        # Skip numbers already globally suppressed so counts reflect real gap-closing.
        existing = set(
            PhoneSuppression.objects.filter(
                organization__isnull=True, phone__in=to_suppress,
            ).values_list('phone', flat=True)
        )
        missing = sorted(to_suppress - existing)

        if not missing:
            self.stdout.write(self.style.SUCCESS(
                'PhoneSuppression already covers every bounced number. Nothing to do.'
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
                defaults={'reason': PhoneSuppression.Reason.BOUNCE},
            )
            created += int(was_created)

        self.stdout.write(self.style.SUCCESS(
            f'Suppressed {created} previously-missed bounced number(s).'
        ))
