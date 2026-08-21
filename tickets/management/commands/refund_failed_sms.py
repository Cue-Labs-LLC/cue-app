"""Reimburse SMS tokens for limit-related failed sends.

Marketing campaigns charge the org's token wallet once, upfront, for the whole audience.
When individual recipients then fail with a limit/config error — 21704 ("The Messaging
Service contains no phone numbers", e.g. the sending number was pulled mid-send) or 30007
(carrier-filtered from exceeding the daily volume) — the org was charged but nothing was
delivered, and today nothing refunds those. This command credits back
ceil(failed_segments × price_per_segment_cents()) per campaign, capped at the campaign's
outstanding net charge, and stamps SMSMessageRecipient.refunded_at so re-runs are no-ops.

Dry-run by default; use --apply to write refunds.

    python manage.py refund_failed_sms --org <org-id>            # preview
    python manage.py refund_failed_sms --org <org-id> --apply    # execute
"""

import logging
import math
from decimal import Decimal, ROUND_CEILING

from django.core.management.base import BaseCommand
from django.db.models import Sum, Count

from tickets.models import SMSCampaign, SMSMessageRecipient
from tickets.services.sms_credits import (
    LIMIT_RELATED_FAIL_ERROR_CODES, price_per_segment_cents, refund_failed_recipients,
)

logger = logging.getLogger(__name__)

_FAILED_STATUSES = [SMSMessageRecipient.Status.FAILED, SMSMessageRecipient.Status.UNDELIVERED]


class Command(BaseCommand):
    help = (
        "Reimburse SMS tokens for limit-related failed sends (21704 no-sender / 30007 "
        "carrier-filtered). Dry-run by default; use --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument('--org', help='Limit to one organization id.')
        parser.add_argument('--campaign', help='Limit to one campaign id.')
        parser.add_argument('--since', help='Only recipients sent on/after this date (YYYY-MM-DD).')
        parser.add_argument(
            '--error-codes',
            help='Comma-separated Twilio error codes to refund (default: 21704,30007).',
        )
        parser.add_argument(
            '--all-failed', action='store_true',
            help='Refund every failed/undelivered send regardless of error code.',
        )
        parser.add_argument(
            '--apply', action='store_true',
            help='Write refunds. Without this flag the command only reports.',
        )

    def handle(self, *args, **options):
        apply = options['apply']
        if options['all_failed']:
            codes = None  # match any error code
        elif options['error_codes']:
            codes = frozenset(c.strip() for c in options['error_codes'].split(',') if c.strip())
        else:
            codes = LIMIT_RELATED_FAIL_ERROR_CODES

        # Eligible (un-refunded) failed recipients, scoped as requested.
        recipients = SMSMessageRecipient.objects.filter(
            status__in=_FAILED_STATUSES, refunded_at__isnull=True, segments__gt=0,
        )
        if codes is not None:
            recipients = recipients.filter(error_code__in=codes)
        else:
            recipients = recipients.exclude(error_code='')
        if options['org']:
            recipients = recipients.filter(campaign__organization_id=options['org'])
        if options['campaign']:
            recipients = recipients.filter(campaign_id=options['campaign'])
        if options['since']:
            recipients = recipients.filter(sent_at__date__gte=options['since'])

        # Group by campaign for a per-campaign summary (matches how the refund is issued).
        rows = (
            recipients.values('campaign_id', 'campaign__name', 'campaign__organization__name')
            .annotate(n=Count('id'), segs=Sum('segments'))
            .order_by('campaign__organization__name', 'campaign__name')
        )
        rows = list(rows)
        if not rows:
            self.stdout.write('No un-refunded limit-related failed sends found.')
            return

        price = price_per_segment_cents()
        total_recipients = 0
        total_cents = 0
        campaign_ids = []
        for r in rows:
            cents = int((price * r['segs']).to_integral_value(rounding=ROUND_CEILING))
            tokens = math.floor(Decimal(cents) / price)
            total_recipients += r['n']
            total_cents += cents
            campaign_ids.append(r['campaign_id'])
            self.stdout.write(
                f"  {r['campaign__organization__name']} · {r['campaign__name']}: "
                f"{r['n']} failed, {r['segs']} segments → {tokens} tokens ({cents}¢)"
            )

        total_tokens = math.floor(Decimal(total_cents) / price)
        if not apply:
            self.stdout.write(self.style.WARNING(
                f"DRY RUN: would reimburse ~{total_tokens} tokens ({total_cents}¢, uncapped) "
                f"across {total_recipients} failed sends in {len(rows)} campaign(s). "
                f"Re-run with --apply."
            ))
            return

        # Apply per campaign via the shared helper (caps at each campaign's outstanding net).
        # For --all-failed (codes is None) resolve the concrete codes so the helper doesn't
        # fall back to its default limit-only set.
        if codes is not None:
            apply_codes = codes
        else:
            apply_codes = frozenset(
                recipients.values_list('error_code', flat=True).distinct()
            )
        applied_recipients = 0
        applied_cents = 0
        for campaign in SMSCampaign.objects.filter(id__in=campaign_ids):
            n, cents = refund_failed_recipients(campaign, error_codes=apply_codes)
            applied_recipients += n
            applied_cents += cents
        applied_tokens = math.floor(Decimal(applied_cents) / price)
        self.stdout.write(self.style.SUCCESS(
            f"Reimbursed {applied_tokens} tokens ({applied_cents}¢) across "
            f"{applied_recipients} failed sends."
        ))
