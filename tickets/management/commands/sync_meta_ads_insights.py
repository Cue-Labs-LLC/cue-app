"""Sync daily Meta Ads Insights for every org with a connected ad account.

Run every few hours by a Render cron. Enqueues one ``sync_meta_ads_insights_task``
per org; each re-syncs the trailing ~28 days and starts the history backfill
chain when older data is still missing.
"""

from django.core.management.base import BaseCommand

from tickets.models import Organization
from tickets.tasks import sync_meta_ads_insights_task


class Command(BaseCommand):
    help = "Sync daily Meta Ads Insights into MetaAdDailyInsight for connected orgs."

    def add_arguments(self, parser):
        parser.add_argument(
            '--organization-id',
            dest='organization_id',
            help='Restrict to a single organization UUID.',
        )
        parser.add_argument(
            '--sync',
            action='store_true',
            help='Run the recent-window sync inline instead of enqueueing (no backfill).',
        )

    def handle(self, *args, **options):
        orgs = Organization.objects.exclude(meta_ads_access_token='').exclude(meta_ads_account_id='')
        if options.get('organization_id'):
            orgs = orgs.filter(pk=options['organization_id'])

        count = 0
        for org_id in orgs.values_list('pk', flat=True):
            if options.get('sync'):
                result = sync_meta_ads_insights_task.apply(args=[str(org_id)], kwargs={'start_backfill': False}).get()
                self.stdout.write(f'{org_id}: {result}')
            else:
                sync_meta_ads_insights_task.delay(str(org_id))
            count += 1
        self.stdout.write(self.style.SUCCESS(
            f"{'Synced' if options.get('sync') else 'Enqueued'} Meta Ads Insights for {count} org(s)."
        ))
