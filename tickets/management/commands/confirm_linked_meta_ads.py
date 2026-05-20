"""Bulk-confirm all currently linked but unconfirmed Meta Ads EventExpense rows."""

from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tickets.models import EventExpense, Organization
from tickets.views import _invalidate_marketing_cache


class Command(BaseCommand):
    help = (
        'Confirm every currently linked (non-deleted) Meta Ads EventExpense '
        'whose confirmed_at is NULL. Dry-run by default; use --apply to write. '
        'confirmed_by is left NULL (system-confirmed, no user attribution).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--org',
            dest='org_slug',
            default=None,
            help='Limit to a specific organization slug (default: all orgs).',
        )
        parser.add_argument(
            '--apply',
            action='store_true',
            default=False,
            help='Write changes to the database (default: dry-run).',
        )

    def handle(self, *args, **options):
        org_slug = options['org_slug']
        apply = options['apply']

        qs = EventExpense.objects.filter(
            source='meta_ads',
            deleted_at__isnull=True,
            confirmed_at__isnull=True,
        ).select_related('event__organization').order_by(
            'event__organization__slug', 'event__start_date',
        )

        if org_slug:
            try:
                Organization.objects.get(slug=org_slug)
            except Organization.DoesNotExist:
                raise CommandError(f'Organization "{org_slug}" not found.')
            qs = qs.filter(event__organization__slug=org_slug)

        expenses = list(qs)

        if not expenses:
            self.stdout.write('No linked unconfirmed Meta Ads expenses found.')
            return

        total_amount = Decimal('0.00')
        self.stdout.write(f'Found {len(expenses)} unconfirmed Meta Ads expense(s):\n')
        for exp in expenses:
            event = exp.event
            org = event.organization
            date = event.start_date.isoformat() if event.start_date else 'n/a'
            self.stdout.write(
                f'  {org.slug:20s}  {date}  ${exp.amount:>10}  '
                f'{event.name[:40]:40s}  {exp.description[:60]}'
            )
            total_amount += exp.amount

        self.stdout.write(f'\nTotal: {len(expenses)} expense(s), ${total_amount}\n')

        if not apply:
            self.stdout.write(self.style.WARNING(
                'DRY-RUN — no changes written. Re-run with --apply to confirm.'
            ))
            return

        now = timezone.now()
        ids_by_org = defaultdict(list)
        for exp in expenses:
            ids_by_org[exp.event.organization].append(exp.pk)

        all_ids = [pk for ids in ids_by_org.values() for pk in ids]
        updated = EventExpense.objects.filter(pk__in=all_ids).update(
            confirmed_at=now, updated_at=now,
        )

        for org in ids_by_org:
            _invalidate_marketing_cache(org)

        self.stdout.write(self.style.SUCCESS(
            f'Confirmed {updated} Meta Ads expense(s) across {len(ids_by_org)} org(s).'
        ))
