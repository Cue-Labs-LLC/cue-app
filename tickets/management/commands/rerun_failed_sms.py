"""Re-run (resend) the failed sends of a specific SMS campaign.

Given one campaign, resend to the recipients that failed — by default the limit-related
failures 21704 ("The Messaging Service contains no phone numbers") and 30007
(carrier-filtered). It creates a NEW campaign to just those recipients via the normal
money path (finalize_campaign_send): the audience is re-materialized (anyone since
opted-out or suppressed is dropped), the wallet is re-charged for what actually goes out,
and a send-now resend dispatches immediately.

Dry-run by default; use --apply to create + send.

    python manage.py rerun_failed_sms --campaign <id>            # preview
    python manage.py rerun_failed_sms --campaign <id> --apply    # resend now
    python manage.py rerun_failed_sms --campaign <id> --schedule 2026-08-25T10:00 --apply

Each run creates a fresh campaign; re-running the same source again resends to the same
failed set — run it once.
"""

import math
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from tickets.models import SMSCampaign
from tickets.services.sms_campaigns import (
    failed_recipient_customer_ids, rerun_failed_recipients,
    AudienceEmptyError, AudienceTooLargeError, DailyCapExceededError,
)
from tickets.services.sms_credits import (
    InsufficientCreditsError, estimate_campaign_cost_cents, price_per_segment_cents,
)


class Command(BaseCommand):
    help = (
        "Resend a campaign's failed sends (default 21704 no-sender / 30007 carrier-filtered) "
        "as a new campaign. Dry-run by default; use --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument('--campaign', required=True, help='Source SMSCampaign id to resend failures from.')
        parser.add_argument(
            '--error-codes',
            help='Comma-separated Twilio error codes to resend (default: 21704,30007).',
        )
        parser.add_argument(
            '--all-failed', action='store_true',
            help='Resend every failed/undelivered recipient regardless of error code.',
        )
        parser.add_argument(
            '--schedule',
            help='Schedule the resend for this local datetime (ISO, e.g. 2026-08-25T10:00) '
                 'instead of sending now.',
        )
        parser.add_argument(
            '--apply', action='store_true',
            help='Create + send the resend. Without this flag the command only reports.',
        )

    def handle(self, *args, **options):
        try:
            source = SMSCampaign.objects.select_related('organization', 'event').get(
                id=options['campaign'])
        except (SMSCampaign.DoesNotExist, ValidationError, ValueError):
            raise CommandError(f'Campaign {options["campaign"]} not found.')

        if options['all_failed']:
            error_codes = frozenset()  # match any error code
        elif options['error_codes']:
            error_codes = frozenset(c.strip() for c in options['error_codes'].split(',') if c.strip())
        else:
            error_codes = None  # helper default: LIMIT_RELATED_FAIL_ERROR_CODES

        # For --all-failed, resolve concrete codes so the helper/count don't fall back to
        # the default limited set.
        resolve_codes = error_codes
        if options['all_failed']:
            from tickets.models import SMSMessageRecipient
            resolve_codes = frozenset(
                SMSMessageRecipient.objects.filter(
                    campaign=source,
                    status__in=[SMSMessageRecipient.Status.FAILED,
                                SMSMessageRecipient.Status.UNDELIVERED],
                    customer_id__isnull=False,
                ).exclude(error_code='').values_list('error_code', flat=True).distinct()
            )

        scheduled = False
        send_at = None
        if options['schedule']:
            parsed = parse_datetime(options['schedule'])
            if not parsed:
                raise CommandError(f'Could not parse --schedule value: {options["schedule"]!r}')
            send_at = timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed
            if send_at <= timezone.now():
                raise CommandError('--schedule must be in the future.')
            scheduled = True

        ids = failed_recipient_customer_ids(source, error_codes=resolve_codes)
        if not ids:
            self.stdout.write('No failed recipients with a contact to resend for this campaign.')
            return

        # True reachable count after opt-out/suppression drop (what will actually send/charge).
        reachable = SMSCampaign(
            organization=source.organization, filter_criteria={}, manual_include_ids=ids,
        ).materialize(source.organization)
        count = len(reachable)
        cents = estimate_campaign_cost_cents(count, source.body)
        tokens = math.floor(Decimal(cents) / price_per_segment_cents()) if cents else 0
        when = 'now' if not scheduled else timezone.localtime(send_at).strftime('%b %d %H:%M')

        self.stdout.write(
            f'Source: {source.organization.name} · {source.name} ({source.id})\n'
            f'  {len(ids)} failed recipient(s) with a contact; '
            f'{count} still reachable → send {when}, ~{tokens} tokens ({cents}¢).'
        )
        if count == 0:
            self.stdout.write(self.style.WARNING(
                'All failed recipients have since opted out or been suppressed — nothing to resend.'))
            return
        if not options['apply']:
            self.stdout.write(self.style.WARNING('DRY RUN: re-run with --apply to send.'))
            return

        try:
            result = rerun_failed_recipients(
                source, error_codes=resolve_codes, scheduled=scheduled, send_at=send_at)
        except AudienceEmptyError:
            self.stdout.write(self.style.WARNING('Nothing to resend (audience empty after filtering).'))
            return
        except AudienceTooLargeError as exc:
            raise CommandError(str(exc))
        except DailyCapExceededError as exc:
            raise CommandError(exc.user_message())
        except InsufficientCreditsError as exc:
            raise CommandError(
                f'Not enough SMS tokens: need {exc.required_cents}¢, have {exc.balance_cents}¢.')

        if result is None:
            self.stdout.write('No failed recipients to resend.')
            return
        self.stdout.write(self.style.SUCCESS(
            f'Resend created: campaign {result.campaign.id} "{result.campaign.name}" — '
            f'{result.recipient_count} recipient(s), {result.cost_tokens} tokens '
            f'({result.cost_cents}¢), {"scheduled" if result.scheduled else "sending now"}.'
        ))
