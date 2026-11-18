"""Meta Ads performance dashboard: campaign + creative metrics for the org's ad account.

Reads live from the Graph API Insights endpoint using the org's existing Meta Ads
connection (see tickets/integrations/meta_ads.py). Nothing is persisted.

Meta rate-limits Insights calls hard (especially on the Limited Marketing API
tier), so the raw Insights pull is cached per date range and everything the
user can change without a new range — creative sort, minimum spend, grouping —
is computed from that cache instead of re-querying Meta. Creative details
and creative keys are cached per account (one dict each, not a key per ad:
hundreds of keys would overflow LocMemCache's 300-entry cull in dev).
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.core.cache import cache as django_cache
from django.db.models import Max, Sum
from django.utils import timezone as django_tz

from ..models import MetaAdDailyInsight

from .meta_ads import (
    MetaAdsAPIError,
    MetaAdsClient,
    _extract_purchase_amount,
    _extract_purchase_count,
)

logger = logging.getLogger(__name__)

# Rolling windows map straight onto Meta date presets. Calendar windows (this
# month, last quarter, custom, ...) come from the shared WINDOW_CHOICES and are
# sent as an explicit time_range (see calendar_date_range).
ROLLING_DATE_PRESETS = [
    ('last_7d', 'Last 7 days'),
    ('last_30d', 'Last 30 days'),
    ('last_90d', 'Last 90 days'),
]
LIFETIME_PRESET = ('maximum', 'Lifetime')
DEFAULT_DATE_PRESET = 'last_30d'
# Meta keeps ~37 months of Insights; older `since` dates are rejected.
INSIGHTS_HISTORY_MONTHS = 36


@dataclass(frozen=True)
class AdsDateRange:
    """The Insights window: a Meta ``date_preset`` or an inclusive since/until."""
    key: str
    label: str
    date_preset: str = ''
    since: date | None = None
    until: date | None = None

    @property
    def time_range(self):
        return (self.since, self.until) if self.since else None

    @property
    def cache_token(self):
        return self.date_preset or f'{self.since:%Y%m%d}-{self.until:%Y%m%d}'

    def bounds(self, today):
        """Inclusive (since, until) dates, matching Meta's preset semantics:
        rolling ``last_Nd`` presets end yesterday; lifetime runs through today."""
        if self.since:
            return self.since, self.until
        if self.date_preset == 'maximum':
            return insights_history_floor(today), today
        days = int(self.date_preset.removeprefix('last_').removesuffix('d'))
        return today - timedelta(days=days), today - timedelta(days=1)


def preset_date_range(key):
    """A rolling/lifetime range, falling back to the default for unknown keys."""
    labels = dict([*ROLLING_DATE_PRESETS, LIFETIME_PRESET])
    key = key if key in labels else DEFAULT_DATE_PRESET
    return AdsDateRange(key=key, label=labels[key], date_preset=key)


def insights_history_floor(today):
    """Earliest date Meta reliably serves Insights for (first of a month ~3 years back)."""
    years_back, month_index = divmod(today.month - 1 - INSIGHTS_HISTORY_MONTHS, 12)
    return date(today.year + years_back, month_index + 1, 1)


def _format_day(d):
    return f'{d:%b} {d.day}, {d.year}'


def calendar_date_range(key, label, start, end, today):
    """Fit calendar-window bounds to what Insights can answer.

    Ads have no future spend, so ``until`` is capped at today (making "This
    month" equivalent to "Month-to-date" here); ``since`` is floored at Meta's
    history limit; a missing start means "as far back as Meta has". Custom
    ranges are labelled with their effective dates.
    """
    floor = insights_history_floor(today)
    until = max(min(end or today, today), floor)
    since = max(start or floor, floor)
    if since > until:
        since = until
    if key == 'custom':
        label = f'{_format_day(since)} \u2013 {_format_day(until)}'
    return AdsDateRange(key=key, label=label, since=since, until=until)

CREATIVE_SORTS = [
    ('spend', 'Spend'),
    ('purchases', 'Purchases'),
    ('roas', 'ROAS'),
    ('cost_per_purchase', 'Cost per purchase'),
    ('ctr', 'CTR'),
]
DEFAULT_CREATIVE_SORT = 'roas'
# Copies of the same video/image are merged into one card unless turned off.
DEFAULT_GROUP_CREATIVES = True
_ASCENDING_SORTS = {'cost_per_purchase'}  # lower is better

# Efficiency sorts (everything but spend) only count creatives above a spend
# floor, so a $5 ad with one lucky purchase can't top the ROAS ranking.
MIN_SPEND_OPTIONS = [25, 100, 250, 1000]
DEFAULT_MIN_SPEND = 100

# Creatives are ranked from every ad that spent at least AD_POOL_MIN_SPEND in the
# window (Meta filters server-side), not just the top N by spend: over a long
# range like "This year" hundreds of ads clear a $100 floor. The floor matches
# the lowest min-spend option, so switching options reuses one cached pull;
# copies of a creative under it don't contribute to that creative's group.
AD_POOL_MIN_SPEND = MIN_SPEND_OPTIONS[0]
AD_POOL_LIMIT = 2000

# Cards shown per page of the grid; "Show more" adds a page, up to the max.
CREATIVES_PAGE_SIZE = 24
MAX_CREATIVES_SHOWN = 96

CACHE_TTL_SECONDS = 600
# Synced daily rows (MetaAdDailyInsight) are used when they cover the requested
# range and the last sync is at most this old; otherwise fall back to live.
SYNCED_DATA_MAX_AGE = timedelta(days=1)
CAMPAIGN_STATUS_TTL_SECONDS = 60 * 60
# Last good pull per range, served (flagged as stale) when Meta rate-limits a
# refresh — the Limited Marketing API tier throttles Insights aggressively.
STALE_BACKUP_TTL_SECONDS = 60 * 60 * 24
CREATIVE_KEY_TTL_SECONDS = 60 * 60 * 24  # an ad's video/image never changes

_METRIC_FIELDS = 'spend,impressions,inline_link_clicks,actions,action_values'
CAMPAIGN_FIELDS = f'campaign_id,campaign_name,account_currency,{_METRIC_FIELDS}'
AD_FIELDS = f'ad_id,ad_name,adset_name,campaign_id,campaign_name,reach,frequency,{_METRIC_FIELDS}'


def resolve_creative_sort(value):
    """Return (sort_key, label), falling back to the default for unknown input."""
    labels = dict(CREATIVE_SORTS)
    key = value if value in labels else DEFAULT_CREATIVE_SORT
    return key, labels[key]


def resolve_creatives_shown(value):
    """Number of creative cards to show: a whole page count, clamped."""
    try:
        pages = int(value) // CREATIVES_PAGE_SIZE
    except (TypeError, ValueError):
        pages = 1
    return max(1, min(pages, MAX_CREATIVES_SHOWN // CREATIVES_PAGE_SIZE)) * CREATIVES_PAGE_SIZE


def resolve_min_spend(value, sort):
    """Spend floor for ``sort``: always 0 for the spend sort, else a known option."""
    if sort == 'spend':
        return 0
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MIN_SPEND
    return amount if amount in MIN_SPEND_OPTIONS else DEFAULT_MIN_SPEND


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value or '0')).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError):
        return Decimal('0.00')


def _to_int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _with_ratios(metrics: dict) -> dict:
    """Add derived ratios. Each is None when its denominator is zero/unknown."""
    spend = metrics['spend']
    impressions = metrics['impressions']
    clicks = metrics['link_clicks']
    purchases = metrics['purchases']
    value = metrics['purchase_value']
    metrics['ctr'] = (Decimal(clicks) * 100 / impressions).quantize(Decimal('0.01')) if impressions else None
    metrics['cpc'] = (spend / clicks).quantize(Decimal('0.01')) if clicks else None
    metrics['cpm'] = (spend * 1000 / impressions).quantize(Decimal('0.01')) if impressions else None
    metrics['cost_per_purchase'] = (spend / purchases).quantize(Decimal('0.01')) if purchases else None
    metrics['roas'] = (value / spend).quantize(Decimal('0.01')) if value is not None and spend else None
    return metrics


def _row_metrics(row: dict) -> dict:
    return _with_ratios({
        'spend': _to_decimal(row.get('spend')),
        'impressions': _to_int(row.get('impressions')),
        'link_clicks': _to_int(row.get('inline_link_clicks')),
        # None = Meta reported no conversion data for this row (no pixel/actions).
        'purchases': _extract_purchase_count(row.get('actions')),
        'purchase_value': _extract_purchase_amount(row.get('action_values')),
    })


def _sum_metrics(rows: list[dict]) -> dict:
    has_conversions = any(r['purchases'] is not None for r in rows)
    return _with_ratios({
        'spend': sum((r['spend'] for r in rows), Decimal('0.00')),
        'impressions': sum(r['impressions'] for r in rows),
        'link_clicks': sum(r['link_clicks'] for r in rows),
        'purchases': sum(r['purchases'] or 0 for r in rows) if has_conversions else None,
        'purchase_value': (
            sum((r['purchase_value'] or Decimal('0.00') for r in rows), Decimal('0.00'))
            if has_conversions else None
        ),
    })


def _synced_metrics(row: dict) -> dict:
    """Metrics from an aggregated MetaAdDailyInsight row. ``purchases`` sums to
    None only when every day lacked conversion data, matching the live path."""
    return _with_ratios({
        'spend': (row['spend'] or Decimal('0.00')).quantize(Decimal('0.01')),
        'impressions': row['impressions'] or 0,
        'link_clicks': row['link_clicks'] or 0,
        'purchases': row['purchases'],
        'purchase_value': (
            row['purchase_value'].quantize(Decimal('0.01')) if row['purchase_value'] is not None else None
        ),
    })


def _ad_from_row(row: dict) -> dict:
    return {
        'id': row.get('ad_id'),
        'name': row.get('ad_name') or 'Untitled ad',
        'adset_name': row.get('adset_name') or '',
        'campaign_name': row.get('campaign_name') or '',
        'reach': _to_int(row.get('reach')),
        'frequency': _to_decimal(row.get('frequency')),
        **_row_metrics(row),
    }


def _single_unit(ad: dict) -> dict:
    """A creative card backed by one ad."""
    return {**ad, 'ads': [ad], 'ad_count': 1}


def _group_units(ads: list[dict], keys: dict[str, str]) -> list[dict]:
    """Merge ads that share a creative (same video/image) into one card each.

    ``ads`` must be sorted by spend descending: each group's first (biggest
    spender) ad represents it — its name, image, copy, and preview. Metrics are
    summed across the group. Reach/frequency are dropped for multi-ad groups
    because reach isn't additive (the same person can see several ads).
    """
    groups: dict[str, list[dict]] = {}
    for ad in ads:
        key = keys.get(ad['id']) or f"ad:{ad['id']}"
        groups.setdefault(key, []).append(ad)

    units = []
    for members in groups.values():
        if len(members) == 1:
            units.append(_single_unit(members[0]))
            continue
        units.append({
            **members[0],
            **_sum_metrics(members),
            'reach': None,
            'frequency': None,
            'ads': members,
            'ad_count': len(members),
        })
    return units


class MetaAdsDashboardCalculator:
    """Campaign and creative performance for the org's connected Meta ad account."""

    def __init__(self, organization, date_range=None,
                 sort=DEFAULT_CREATIVE_SORT, min_spend=None, group=DEFAULT_GROUP_CREATIVES,
                 shown=CREATIVES_PAGE_SIZE):
        self.organization = organization
        self.shown = resolve_creatives_shown(shown)
        self.date_range = date_range or preset_date_range(DEFAULT_DATE_PRESET)
        self.sort, self.sort_label = resolve_creative_sort(sort)
        self.min_spend = resolve_min_spend(min_spend, self.sort)
        self.group = bool(group)
        self._client = None
        self.stale_fetched_at = None  # set when serving a backup during a rate limit

    @property
    def client(self):
        if self._client is None:
            self._client = MetaAdsClient(self.organization.meta_ads_access_token)
        return self._client

    @property
    def _cache_prefix(self):
        org = self.organization
        return f'meta_ads:{org.pk}:{org.meta_ads_account_id}'

    def calculate(self, refresh=False) -> dict:
        """Raises MetaAdsAPIError if Meta rejects the request (e.g. expired token)."""
        performance = (
            self._synced_performance() if self.uses_synced_data() else self._live_performance(refresh)
        )
        campaigns = performance['campaigns']
        campaigns.sort(key=lambda c: c['spend'], reverse=True)
        ads = performance['ads']
        ads.sort(key=lambda a: a['spend'], reverse=True)

        if self.group:
            keys = self._creative_keys([a['id'] for a in ads if a['id']])
            units = _group_units(ads, keys)
        else:
            units = [_single_unit(ad) for ad in ads]
        if self.min_spend:
            units = [u for u in units if u['spend'] >= self.min_spend]
        units.sort(key=self._sort_key)
        creatives = units[:self.shown]
        self._attach_details(creatives, refresh)

        currency = performance['currency']
        return {
            'currency': currency,
            'currency_prefix': '$' if currency in ('', 'USD', 'CAD', 'AUD') else f'{currency} ',
            'totals': _sum_metrics(campaigns),
            'campaigns': campaigns,
            'creatives': creatives,
            'qualifying_creatives': len(units),
            'has_more_creatives': len(units) > self.shown and self.shown < MAX_CREATIVES_SHOWN,
            'pool_min_spend': AD_POOL_MIN_SPEND,
            # Set when Meta rate-limited the pull and we fell back to older data.
            'stale_fetched_at': self.stale_fetched_at,
            # True when more ads cleared the pool floor than we pull.
            'ad_pool_capped': len(ads) >= AD_POOL_LIMIT,
            # 'synced' (summed from MetaAdDailyInsight) or 'live' (Insights API).
            'source': performance['source'],
            'synced_at': performance.get('synced_at'),
        }

    def uses_synced_data(self, today=None) -> bool:
        """True when the daily sync fully covers this range and is fresh."""
        org = self.organization
        if not (org.meta_ads_synced_since and org.meta_ads_synced_at):
            return False
        if django_tz.now() - org.meta_ads_synced_at > SYNCED_DATA_MAX_AGE:
            return False
        since, _ = self.date_range.bounds(today or django_tz.localdate())
        return since >= org.meta_ads_synced_since

    def _synced_performance(self) -> dict:
        """Campaign + ad metrics summed from synced daily rows (no Insights calls).

        Reach/frequency aren't stored (not additive across days), so they're
        unavailable here and render as "-".
        """
        org = self.organization
        since, until = self.date_range.bounds(django_tz.localdate())
        rows = MetaAdDailyInsight.objects.filter(
            organization=org, ad_account_id=org.meta_ads_account_id, date__range=(since, until),
        )
        sums = {
            'spend': Sum('spend'),
            'impressions': Sum('impressions'),
            'link_clicks': Sum('link_clicks'),
            'purchases': Sum('purchases'),
            'purchase_value': Sum('purchase_value'),
        }
        statuses = self._campaign_statuses()
        campaigns = [
            {
                'id': row['campaign_id'],
                'name': row['name'] or 'Untitled campaign',
                'status': statuses.get(row['campaign_id'], ''),
                **_synced_metrics(row),
            }
            for row in rows.values('campaign_id').annotate(name=Max('campaign_name'), **sums)
        ]
        ad_rows = (
            rows.values('ad_id')
            .annotate(
                name=Max('ad_name'), adset=Max('adset_name'), campaign=Max('campaign_name'), **sums,
            )
            .filter(spend__gte=AD_POOL_MIN_SPEND)
            .order_by('-spend')[:AD_POOL_LIMIT]
        )
        ads = [
            {
                'id': row['ad_id'],
                'name': row['name'] or 'Untitled ad',
                'adset_name': row['adset'] or '',
                'campaign_name': row['campaign'] or '',
                'reach': None,
                'frequency': None,
                **_synced_metrics(row),
            }
            for row in ad_rows
        ]
        currency = rows.exclude(currency='').values_list('currency', flat=True).first() or ''
        return {
            'campaigns': campaigns, 'ads': ads, 'currency': currency,
            'source': 'synced', 'synced_at': org.meta_ads_synced_at,
        }

    def _live_performance(self, refresh) -> dict:
        """Campaign + ad metrics straight from the Insights API (cached)."""
        raw = self._raw_insights(refresh)
        campaigns = [
            {
                'id': row.get('campaign_id'),
                'name': row.get('campaign_name') or 'Untitled campaign',
                'status': raw['statuses'].get(row.get('campaign_id'), ''),
                **_row_metrics(row),
            }
            for row in raw['campaign_rows']
        ]
        currency = next(
            (r.get('account_currency') for r in raw['campaign_rows'] if r.get('account_currency')), '',
        )
        return {
            'campaigns': campaigns,
            'ads': [_ad_from_row(row) for row in raw['ad_rows']],
            'currency': currency,
            'source': 'live',
        }

    def _campaign_statuses(self) -> dict:
        """Campaign id -> status, cached. Best-effort: badges are omitted if
        Meta can't be reached, rather than failing a page that needs no Insights."""
        cache_key = f'{self._cache_prefix}:campaign_statuses:v1'
        statuses = django_cache.get(cache_key)
        if statuses is None:
            try:
                campaigns = self.client.list_campaigns(self.organization.meta_ads_account_id)
            except MetaAdsAPIError:
                logger.warning('Meta Ads dashboard: could not load campaign statuses', exc_info=True)
                return {}
            statuses = {c.get('id'): c.get('status', '') for c in campaigns}
            django_cache.set(cache_key, statuses, CAMPAIGN_STATUS_TTL_SECONDS)
        return statuses

    def _sort_key(self, unit):
        """Sort by the chosen metric; creatives missing it go last; ties by spend."""
        value = unit.get(self.sort)
        if value is None:
            return (1, Decimal(0), -unit['spend'])
        return (0, value if self.sort in _ASCENDING_SORTS else -value, -unit['spend'])

    def _raw_insights(self, refresh) -> dict:
        """Campaign + ad Insights for the date range, cached across sort/group changes."""
        cache_key = f'{self._cache_prefix}:raw:v2:{self.date_range.cache_token}'
        backup_key = f'{self._cache_prefix}:raw_backup:v2:{self.date_range.cache_token}'
        if not refresh:
            cached = django_cache.get(cache_key)
            if cached is not None:
                return cached
        try:
            raw = self._fetch_raw_insights()
        except MetaAdsAPIError as exc:
            backup = django_cache.get(backup_key) if exc.is_rate_limited else None
            if backup is None:
                raise
            logger.info('Meta Ads dashboard: rate limited, serving data from %s', backup['fetched_at'])
            self.stale_fetched_at = backup['fetched_at']
            return backup
        django_cache.set(cache_key, raw, CACHE_TTL_SECONDS)
        django_cache.set(backup_key, raw, STALE_BACKUP_TTL_SECONDS)
        return raw

    def _fetch_raw_insights(self) -> dict:

        # Independent calls; ad-level Insights alone takes several seconds on a
        # large account, so run them concurrently rather than back to back.
        account_id = self.organization.meta_ads_account_id
        window = {
            'date_preset': self.date_range.date_preset,
            'time_range': self.date_range.time_range,
        }
        with ThreadPoolExecutor(max_workers=3) as pool:
            campaign_rows = pool.submit(
                self.client.get_account_insights, account_id, level='campaign',
                fields=CAMPAIGN_FIELDS, sort='spend_descending', **window,
            )
            ad_rows = pool.submit(
                self.client.get_account_insights, account_id, level='ad',
                fields=AD_FIELDS, sort='spend_descending', limit=AD_POOL_LIMIT,
                min_spend=AD_POOL_MIN_SPEND, **window,
            )
            campaigns = pool.submit(self.client.list_campaigns, account_id)
            raw = {
                'campaign_rows': campaign_rows.result(),
                'ad_rows': ad_rows.result(),
                'statuses': {c.get('id'): c.get('status', '') for c in campaigns.result()},
                'fetched_at': django_tz.now(),
            }
        return raw

    def _creative_keys(self, ad_ids: list[str]) -> dict[str, str]:
        """Ad id -> creative key ('' = ungroupable), cached per account for a day.
        Best-effort: if Meta fails, uncached ads just stay ungrouped."""
        cache_key = f'{self._cache_prefix}:creative_keys:v1'
        keys = django_cache.get(cache_key) or {}
        missing = [ad_id for ad_id in ad_ids if ad_id not in keys]
        if missing:
            try:
                fetched = self.client.get_ad_creative_keys(
                    self.organization.meta_ads_account_id, missing,
                )
            except MetaAdsAPIError:
                logger.warning('Meta Ads dashboard: could not load creative keys', exc_info=True)
                return keys
            # Record misses as '' too so they aren't re-requested every view.
            keys = {**keys, **{ad_id: fetched.get(ad_id, '') for ad_id in missing}}
            django_cache.set(cache_key, keys, CREATIVE_KEY_TTL_SECONDS)
        return keys

    def _attach_details(self, creatives, refresh):
        """Add image, copy, and status from each card's representative ad.

        Cached per account so re-sorting doesn't re-fetch; entries expire
        individually after CACHE_TTL_SECONDS. Best-effort: a failure leaves
        cards without images/copy rather than taking down the dashboard.
        """
        for creative in creatives:
            creative.update({'detail': {}, 'thumbnail_url': '', 'preview_ad_id': ''})

        cache_key = f'{self._cache_prefix}:details:v1'
        now = time.time()
        stored = {} if refresh else (django_cache.get(cache_key) or {})
        stored = {ad_id: e for ad_id, e in stored.items() if now - e['at'] < CACHE_TTL_SECONDS}
        ad_ids = [c['id'] for c in creatives if c['id']]
        missing = [ad_id for ad_id in ad_ids if ad_id not in stored]
        if missing:
            try:
                fetched = self.client.get_ad_details(missing)
            except MetaAdsAPIError:
                logger.warning('Meta Ads dashboard: could not load creative details', exc_info=True)
                fetched = {}
            if fetched:
                stored.update({ad_id: {'at': now, 'detail': d} for ad_id, d in fetched.items()})
                django_cache.set(cache_key, stored, CACHE_TTL_SECONDS)

        for creative in creatives:
            detail = (stored.get(creative['id']) or {}).get('detail') or {}
            creative['detail'] = detail
            creative['thumbnail_url'] = detail.get('image_url', '')
            # Video ads play via the in-panel preview endpoint (numeric ids only,
            # matching its <int:ad_id> route).
            if detail.get('is_video') and str(creative['id'] or '').isdigit():
                creative['preview_ad_id'] = creative['id']
