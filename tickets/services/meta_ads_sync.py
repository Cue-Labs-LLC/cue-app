"""Sync daily ad-level Meta Insights into MetaAdDailyInsight.

The Meta Ads dashboard sums these rows for any date range instead of querying
Meta live (see MetaAdsDashboardCalculator). Two jobs:

- ``sync_recent``: re-pull the trailing SYNC_LOOKBACK_DAYS. Meta keeps revising
  a day's conversions while they're inside its attribution window, so recent
  days are replaced wholesale on every run rather than appended.
- ``backfill_previous_month``: extend coverage one month further back, down to
  Meta's ~3-year history floor. Run as a spaced-out chain of tasks so a full
  backfill doesn't trip the Insights rate limit.

Coverage is tracked on the org: ``meta_ads_synced_since`` is the earliest date
the table covers with no gaps up to the last sync (``meta_ads_synced_at``).
"""

import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone as django_tz

from ..models import MetaAdDailyInsight
from .meta_ads import MetaAdsClient, _extract_purchase_amount, _extract_purchase_count, _extract_results_count
from .meta_ads_dashboard import insights_history_floor

logger = logging.getLogger(__name__)

SYNC_LOOKBACK_DAYS = 28
DAILY_FIELDS = (
    'ad_id,ad_name,adset_name,campaign_id,campaign_name,account_currency,'
    'spend,impressions,inline_link_clicks,actions,action_values'
)
# Daily rows = ads x days; generous so a busy month isn't truncated.
MAX_ROWS_PER_PULL = 50000


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value or '0')).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError):
        return Decimal('0.00')


def _int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


class MetaAdsInsightsSync:
    """Pull daily ad Insights for one org's connected ad account into the DB."""

    def __init__(self, organization, client=None):
        self.organization = organization
        self.account_id = organization.meta_ads_account_id
        self.client = client or MetaAdsClient(organization.meta_ads_access_token)

    def sync_recent(self, today=None) -> int:
        """Replace the trailing SYNC_LOOKBACK_DAYS (through today) and mark the sync."""
        today = today or django_tz.localdate()
        since = max(today - timedelta(days=SYNC_LOOKBACK_DAYS - 1), insights_history_floor(today))
        count = self.sync_range(since, today)

        org = self.organization
        org.meta_ads_synced_at = django_tz.now()
        if org.meta_ads_synced_since is None or org.meta_ads_synced_since > since:
            org.meta_ads_synced_since = since
        org.save(update_fields=['meta_ads_synced_at', 'meta_ads_synced_since'])
        return count

    def backfill_previous_month(self, today=None):
        """Extend coverage back through the month before ``meta_ads_synced_since``.

        Returns the number of rows synced, or None when there's nothing left to
        backfill (never synced, or already at Meta's history floor).
        """
        today = today or django_tz.localdate()
        synced_since = self.organization.meta_ads_synced_since
        floor = insights_history_floor(today)
        if synced_since is None or synced_since <= floor:
            return None
        until = synced_since - timedelta(days=1)
        since = max(until.replace(day=1), floor)
        count = self.sync_range(since, until)

        self.organization.meta_ads_synced_since = since
        self.organization.save(update_fields=['meta_ads_synced_since'])
        return count

    def is_backfilled(self, today=None) -> bool:
        today = today or django_tz.localdate()
        synced_since = self.organization.meta_ads_synced_since
        return synced_since is not None and synced_since <= insights_history_floor(today)

    def sync_range(self, since, until) -> int:
        """Replace every row in ``since..until`` (inclusive) with Meta's current numbers."""
        rows = self.client.get_account_insights(
            self.account_id, level='ad', fields=DAILY_FIELDS,
            time_range=(since, until), time_increment=1, limit=MAX_ROWS_PER_PULL,
        )
        records = [self._record(row) for row in rows if row.get('ad_id') and row.get('date_start')]
        with transaction.atomic():
            MetaAdDailyInsight.objects.filter(
                organization=self.organization,
                ad_account_id=self.account_id,
                date__range=(since, until),
            ).delete()
            MetaAdDailyInsight.objects.bulk_create(records, batch_size=1000)
        logger.info(
            'Meta Ads sync: org=%s account=%s %s..%s rows=%d',
            self.organization.pk, self.account_id, since, until, len(records),
        )
        return len(records)

    def _record(self, row: dict) -> MetaAdDailyInsight:
        return MetaAdDailyInsight(
            organization=self.organization,
            ad_account_id=self.account_id,
            date=row['date_start'],
            ad_id=row['ad_id'],
            ad_name=(row.get('ad_name') or '')[:512],
            adset_name=(row.get('adset_name') or '')[:512],
            campaign_id=row.get('campaign_id') or '',
            campaign_name=(row.get('campaign_name') or '')[:512],
            currency=(row.get('account_currency') or '')[:3],
            spend=_decimal(row.get('spend')),
            impressions=_int(row.get('impressions')),
            link_clicks=_int(row.get('inline_link_clicks')),
            purchases=_extract_purchase_count(row.get('actions')),
            purchase_value=_extract_purchase_amount(row.get('action_values')),
            results=_extract_results_count(row.get('actions')),
        )


def synced_account_differs(organization, account_id) -> bool:
    """True if the org has synced rows (or coverage) for an account other than ``account_id``."""
    rows = MetaAdDailyInsight.objects.filter(organization=organization)
    if rows.exclude(ad_account_id=account_id).exists():
        return True
    return organization.meta_ads_synced_since is not None and not rows.exists()


def reset_meta_ads_sync(organization):
    """Forget synced data, e.g. when the org switches or disconnects its ad account.
    Clears the coverage fields on ``organization``; the caller saves it."""
    MetaAdDailyInsight.objects.filter(organization=organization).delete()
    organization.meta_ads_synced_since = None
    organization.meta_ads_synced_at = None
