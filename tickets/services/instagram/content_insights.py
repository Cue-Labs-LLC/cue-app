"""Organic Instagram content performance for the org's connected account.

Reads live from ``graph.instagram.com`` (Instagram API with Instagram Login) using the
same connection the DM agent uses — nothing is persisted. The last 12 months are pulled
as weekly time slices fetched in parallel, each one request with the posts' lifetime
insights inlined via field expansion — rather than one insights call per post. Measured
on a 272-post year: ~30s per-post (6 workers), ~7s with monthly slices (Meta's response
time grows with posts per request, so the busiest month dominated), ~4-5s weekly. That single 12-month pull is
cached per (account, token) and every window, sort, filter, and grouping is derived from
it, so switching ranges never hits Meta again; Refresh bypasses the cache. If a slice's
expanded request fails, that slice falls back to listing posts + per-post insights calls.

Insights need the ``instagram_business_manage_insights`` scope. An account connected
before that scope was requested still gets the post list + like/comment counts (both
come from the media node under ``instagram_business_basic``), and the page asks the
organizer to reconnect for the rest.

Pattern: ``InstagramContentInsightsCalculator(org, window=..., sort=...).calculate()``.
"""

import hashlib
import logging
import statistics
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from django.core.cache import cache as django_cache
from django.utils import timezone as django_tz

from .graph_client import InstagramGraphAPIError, InstagramGraphClient

logger = logging.getLogger(__name__)

WINDOW_CHOICES = [
    ('30d', 'Last 30 days', 30),
    ('90d', 'Last 90 days', 90),
    ('180d', 'Last 6 months', 180),
    ('365d', 'Last 12 months', 365),
]
DEFAULT_WINDOW = '90d'
# Every window is cut from one pull covering the longest window.
FETCH_DAYS = max(days for _key, _label, days in WINDOW_CHOICES)
# The pull is split into slices of this many days, all fetched concurrently: Meta pages
# /me/media sequentially and its response time grows with the posts per request, so
# many small slices beat a few large ones. 53 weekly slices cover the year in one wave.
SLICE_DAYS = 7
SLICE_WORKERS = 53
# Safety bound on posts per pull (~3 posts/day for a year). Hitting it is surfaced on
# the page, never silent.
MAX_POSTS = 1000
# Per-post insights calls, used only by the fallback path.
INSIGHTS_WORKERS = 8
TOP_POSTS = 4
# Instagram finalizes a post's numbers over ~48h, so an hour-old pull is still current.
CACHE_TTL_SECONDS = 60 * 60

# Lifetime metrics per product type. Requesting a metric the type doesn't support errors
# the whole call, so each type asks only for what Meta documents for it, falling back to
# CORE_METRICS if the richer set is rejected (e.g. a metric still "in development").
REEL_METRICS = (
    'reach', 'views', 'likes', 'comments', 'shares', 'saved', 'total_interactions',
    'ig_reels_avg_watch_time',
)
FEED_METRICS = (
    'reach', 'views', 'likes', 'comments', 'shares', 'saved', 'total_interactions',
    'profile_visits', 'follows',
)
CORE_METRICS = ('reach', 'likes', 'comments', 'shares', 'saved', 'total_interactions')
# Requested together through field expansion: Meta returns only the metrics each post's
# type supports instead of erroring, so one request covers every format.
ALL_METRICS = tuple(dict.fromkeys(FEED_METRICS + REEL_METRICS))

FORMAT_REEL = 'reel'
FORMAT_PHOTO = 'photo'
FORMAT_CAROUSEL = 'carousel'
FORMAT_LABELS = OrderedDict([
    (FORMAT_REEL, 'Reels'),
    (FORMAT_PHOTO, 'Photos'),
    (FORMAT_CAROUSEL, 'Carousels'),
])
FORMAT_SINGULAR = {FORMAT_REEL: 'Reel', FORMAT_PHOTO: 'Photo', FORMAT_CAROUSEL: 'Carousel'}

# Sort key -> (label, row field). All sorts are descending except "published".
SORTS = OrderedDict([
    ('published', ('Newest', 'published_at')),
    ('reach', ('Reach', 'reach')),
    ('views', ('Views', 'views')),
    ('engagement_rate', ('Engagement rate', 'engagement_rate')),
    ('interactions', ('Interactions', 'interactions')),
    ('shares', ('Shares', 'shares')),
    ('saves', ('Saves', 'saves')),
    ('follows', ('Follows', 'follows')),
])
DEFAULT_SORT = 'published'

# Chart grouping: one bar per post, or posts rolled up into calendar weeks / months.
GROUPS = OrderedDict([
    ('post', 'Post'),
    ('week', 'Week'),
    ('month', 'Month'),
])
DEFAULT_GROUP = 'post'

# The detail panel's "How it compares" needs this many peer posts in a group before it
# shows a typical value for it — below that, one post decides the median.
MIN_POSTS_TO_COMPARE = 3

# Insight status per post.
INSIGHTS_OK = 'ok'
INSIGHTS_PERMISSION = 'permission'
INSIGHTS_UNAVAILABLE = 'unavailable'


def resolve_window(key):
    """``(key, label, days)`` for a window key, falling back to the default."""
    for choice in WINDOW_CHOICES:
        if choice[0] == key:
            return choice
    return next(c for c in WINDOW_CHOICES if c[0] == DEFAULT_WINDOW)


def classify_format(media):
    """Map a media node onto Reels / Photos / Carousels.

    ``media_product_type`` is the precise signal, but it isn't always returned under
    Instagram Login, so fall back to ``media_type``: every video published to the feed
    since 2022 is a reel.
    """
    product = (media.get('media_product_type') or '').upper()
    media_type = (media.get('media_type') or '').upper()
    if product == 'REELS' or media_type == 'VIDEO':
        return FORMAT_REEL
    if media_type == 'CAROUSEL_ALBUM':
        return FORMAT_CAROUSEL
    return FORMAT_PHOTO


def _parse_timestamp(raw):
    # Meta sends e.g. "2026-11-02T18:04:11+0000" (no colon in the offset).
    if not raw:
        return None
    try:
        return datetime.strptime(raw, '%Y-%m-%dT%H:%M:%S%z')
    except ValueError:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None


def _rate(numerator, denominator):
    if not denominator or numerator is None:
        return None
    return round(numerator / denominator * 100, 1)


def _avg(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return round(sum(values) / len(values))


def _sum(values):
    values = [v for v in values if v is not None]
    return sum(values) if values else None


class InstagramContentInsightsCalculator:
    """Per-post and per-format organic metrics for one org's Instagram account."""

    def __init__(self, organization, window=None, sort=None, fmt=None, group=None, client=None):
        self.organization = organization
        self.window, self.window_label, self.window_days = resolve_window(window)
        self.sort = sort if sort in SORTS else DEFAULT_SORT
        self.format_filter = fmt if fmt in FORMAT_LABELS else ''
        self.group = group if group in GROUPS else DEFAULT_GROUP
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = InstagramGraphClient(self.organization.instagram_page_access_token)
        return self._client

    @property
    def sort_label(self):
        return SORTS[self.sort][0]

    # -- public --------------------------------------------------------------

    def calculate(self, refresh=False):
        """Build the page payload. Raises ``InstagramGraphAPIError`` if the account
        or media list can't be read (per-post insight failures degrade gracefully)."""
        raw = self._raw(refresh=refresh)
        cutoff = raw['fetched_at'] - timedelta(days=self.window_days)
        in_window = [item for item in raw['posts'] if item['published_at'] >= cutoff]
        # A capped pull only cuts this window short if a dropped post falls inside it.
        dropped = raw['newest_dropped_at']
        truncated = dropped is not None and dropped >= cutoff
        posts = [self._build_row(item, raw['followers']) for item in in_window]
        self._attach_events(posts)
        posts_with_insights = [p for p in posts if p['has_insights']]

        median_reach = None
        reaches = [p['reach'] for p in posts_with_insights if p['reach'] is not None]
        if reaches:
            median_reach = statistics.median(reaches)
        for post in posts:
            post['reach_index'] = (
                round(post['reach'] / median_reach, 1)
                if median_reach and post['reach'] is not None else None
            )

        visible = [p for p in posts if not self.format_filter or p['format'] == self.format_filter]
        periods = self._periods(visible, raw['fetched_at']) if self.group != 'post' else []
        # A "top posts" strip only means something with enough posts to rank.
        ranked = [p for p in visible if p['has_insights'] and p['reach'] is not None]
        top_posts = (
            sorted(ranked, key=lambda p: p['reach'], reverse=True)[:TOP_POSTS]
            if len(ranked) > TOP_POSTS else []
        )
        return {
            'account': raw['account'],
            'followers': raw['followers'],
            'fetched_at': raw['fetched_at'],
            'insights_permission_missing': any(
                p['insights_status'] == INSIGHTS_PERMISSION for p in posts
            ),
            'unavailable_count': sum(
                1 for p in posts if p['insights_status'] == INSIGHTS_UNAVAILABLE
            ),
            'truncated': truncated,
            'max_posts': MAX_POSTS,
            'post_count': len(posts),
            'totals': self._totals(posts, posts_with_insights, median_reach, raw['followers']),
            'formats': self._format_breakdown(posts_with_insights, posts),
            'posts': self._sorted(visible),
            'top_posts': top_posts,
            # The chart follows the format filter (like the posts table); its dashed
            # baseline stays the all-posts median so "beats your typical post" means the
            # same thing on every filter, matching the table's "vs median" column.
            'chart': (
                self._chart([p for p in visible if p['has_insights']], median_reach)
                if self.group == 'post' else self._period_chart(periods)
            ),
            'periods': periods,
        }

    def post_detail(self, media_id):
        """Everything the detail panel shows for one post, or ``None`` if it isn't in
        the current window's pull.

        Benchmarks compare the post with the window's medians across all posts and the
        same format, so "1,200 reach" reads as good or bad at a glance. Carousel slides are fetched on demand (one call, cached an hour).
        """
        data = self.calculate()
        post = next((p for p in data['posts'] if p['id'] == media_id), None)
        if post is None:
            return None
        measured = [p for p in data['posts'] if p['has_insights'] and p['reach'] is not None]

        def benchmark(label, peers):
            peers = [p for p in peers if p['id'] != media_id]
            if len(peers) < MIN_POSTS_TO_COMPARE:
                return None
            reach = statistics.median(p['reach'] for p in peers)
            rates = [p['engagement_rate'] for p in peers if p['engagement_rate'] is not None]
            return {
                'label': label,
                'peers': len(peers),
                'median_reach': round(reach),
                'reach_ratio': round(post['reach'] / reach, 1) if reach and post['reach'] is not None else None,
                'median_engagement_rate': round(statistics.median(rates), 1) if rates else None,
            }

        benchmarks = [b for b in (
            benchmark('All posts', measured),
            benchmark(f"{FORMAT_LABELS[post['format']]}", [p for p in measured if p['format'] == post['format']]),
        ) if b]

        return {
            'post': post,
            'benchmarks': benchmarks,
            'slides': self._carousel_slides(post) if post['format'] == FORMAT_CAROUSEL else [],
            'followers': data['followers'],
            'window_label': self.window_label,
        }

    def _carousel_slides(self, post):
        key = f"ig_carousel:{self.organization.pk}:{post['id']}"
        slides = django_cache.get(key)
        if slides is None:
            try:
                children = self.client.get_media_children(post['id'])
            except InstagramGraphAPIError as exc:
                logger.info("Instagram carousel slides unavailable for %s: %s", post['id'], exc)
                return []
            slides = [
                {
                    'is_video': (child.get('media_type') or '').upper() == 'VIDEO',
                    'media_url': child.get('media_url') or '',
                    'thumbnail_url': child.get('thumbnail_url') or '',
                }
                for child in children
            ]
            django_cache.set(key, slides, CACHE_TTL_SECONDS)
        return slides

    def rows_for_media_ids(self, media_ids):
        """Fully built post rows (metrics, reach index, labels, events) for ``media_ids``.

        Posts in the cached 12-month pull are free; older ones are fetched one by one
        (cached an hour). Reach index is against the 12-month median. Ids Instagram no
        longer returns (deleted posts) are skipped and reported in ``missing``.
        """
        raw = self._raw()
        followers = raw['followers']
        by_id = {item['id']: item for item in raw['posts']}
        items, missing = [], []
        for media_id in media_ids:
            item = by_id.get(media_id) or self._fetch_single(media_id)
            if item is None:
                missing.append(media_id)
            else:
                items.append(item)
        rows = [self._build_row(item, followers) for item in items]
        self._attach_events(rows)
        reaches = [
            item['insights']['reach'] for item in raw['posts']
            if item.get('insights_status') == INSIGHTS_OK
            and isinstance((item.get('insights') or {}).get('reach'), (int, float))
        ]
        median_reach = statistics.median(reaches) if reaches else None
        for row in rows:
            row['reach_index'] = (
                round(row['reach'] / median_reach, 1)
                if median_reach and row['reach'] is not None else None
            )
        return rows, missing

    def _fetch_single(self, media_id):
        key = f"ig_media:{self.organization.pk}:{media_id}"
        cached = django_cache.get(key)
        if cached is not None:
            return cached or None
        try:
            item = self.client.get_media_with_insights(media_id, ALL_METRICS)
        except InstagramGraphAPIError as exc:
            if exc.is_token_error or exc.is_rate_limited:
                raise
            logger.info("Instagram media %s unavailable: %s", media_id, exc)
            django_cache.set(key, {}, CACHE_TTL_SECONDS)
            return None
        prepared = self._prepare([item])
        if not prepared:
            return None
        prepared = prepared[0]
        metrics = prepared.get('insights')
        prepared['insights_status'] = INSIGHTS_OK if metrics else INSIGHTS_UNAVAILABLE
        prepared['insights'] = metrics or {}
        django_cache.set(key, prepared, CACHE_TTL_SECONDS)
        return prepared

    def raw_posts(self, refresh=False):
        """The cached 12-month pull's post dicts (newest first), for background jobs."""
        return self._raw(refresh=refresh)['posts']

    # -- linked events ---------------------------------------------------------

    def _attach_events(self, posts):
        """Add the events each post is linked to (one query for all posts)."""
        from ...models import InstagramPostEvent

        events = {}
        for link in (InstagramPostEvent.objects
                     .filter(organization=self.organization,
                             ig_media_id__in=[p['id'] for p in posts],
                             event__deleted_at__isnull=True)
                     .select_related('event').order_by('event__start_date')):
            events.setdefault(link.ig_media_id, []).append(link.event)
        for post in posts:
            post['events'] = events.get(post['id'], [])

    # -- fetching ------------------------------------------------------------

    def _cache_key(self):
        # One pull per account serves every window. Keyed on a token fingerprint so
        # reconnecting (new token, possibly new scopes) never serves an old pull.
        token = self.organization.instagram_page_access_token or ''
        fingerprint = hashlib.sha256(token.encode()).hexdigest()[:12]
        return (
            f'ig_content:v2:{self.organization.pk}:'
            f'{self.organization.instagram_business_account_id}:{fingerprint}'
        )

    def _raw(self, refresh=False):
        key = self._cache_key()
        if not refresh:
            cached = django_cache.get(key)
            if cached is not None:
                return cached
        raw = self._fetch()
        django_cache.set(key, raw, CACHE_TTL_SECONDS)
        return raw

    def _fetch(self):
        account = self.client.get_account_profile()
        now = django_tz.now()
        since = now - timedelta(days=FETCH_DAYS)
        slices = []
        start = since
        while start < now:
            end = min(start + timedelta(days=SLICE_DAYS), now)
            slices.append((start, end))
            start = end

        # Errors that make the whole pull meaningless (expired token, rate limit)
        # propagate out of pool.map; anything else is handled per slice.
        with ThreadPoolExecutor(max_workers=SLICE_WORKERS) as pool:
            slice_results = list(pool.map(lambda sl: self._fetch_slice(*sl), slices))

        posts, seen = [], set()
        for items in slice_results:
            for item in items:
                # since/until are inclusive, so a post exactly on a boundary can come
                # back in two slices.
                if item['id'] in seen or item['published_at'] < since:
                    continue
                seen.add(item['id'])
                posts.append(item)
        posts.sort(key=lambda p: p['published_at'], reverse=True)
        newest_dropped_at = posts[MAX_POSTS]['published_at'] if len(posts) > MAX_POSTS else None
        posts = posts[:MAX_POSTS]

        followers = account.get('followers_count')
        return {
            'account': {
                'username': account.get('username') or self.organization.instagram_username,
                'profile_picture_url': account.get('profile_picture_url') or '',
                'media_count': account.get('media_count'),
            },
            'followers': followers if isinstance(followers, int) else None,
            'posts': posts,
            'newest_dropped_at': newest_dropped_at,
            'fetched_at': now,
        }

    def _fetch_slice(self, since, until):
        """Posts published in [since, until] with their insights attached."""
        try:
            media = self.client.list_media_with_insights(
                metrics=ALL_METRICS, since=since, until=until, max_items=MAX_POSTS + 1,
            )
            items = self._prepare(media)
            for item in items:
                metrics = item.get('insights')
                item['insights_status'] = INSIGHTS_OK if metrics else INSIGHTS_UNAVAILABLE
                item['insights'] = metrics or {}
            return items
        except InstagramGraphAPIError as exc:
            if exc.is_token_error or exc.is_rate_limited:
                raise
            fallback_reason = exc

        items = self._prepare(
            self.client.list_media(since=since, until=until, max_items=MAX_POSTS + 1)
        )
        if fallback_reason.is_permission_error:
            # No insights scope: per-post calls would fail the same way, so don't make
            # them. Likes/comments still come from the media node.
            for item in items:
                item['insights_status'] = INSIGHTS_PERMISSION
                item['insights'] = {}
            return items

        logger.info(
            "Instagram expanded insights failed for %s..%s (%s); falling back to per-post",
            since.date(), until.date(), fallback_reason,
        )
        with ThreadPoolExecutor(max_workers=INSIGHTS_WORKERS) as pool:
            results = list(pool.map(self._fetch_insights, items))
        for item, (status, metrics) in zip(items, results):
            item['insights_status'] = status
            item['insights'] = metrics
        return items

    @staticmethod
    def _prepare(media):
        """Parse timestamps + formats; drop items without a usable timestamp."""
        items = []
        for item in media:
            published_at = _parse_timestamp(item.get('timestamp'))
            if published_at is None:
                continue
            items.append(dict(item, published_at=published_at, format=classify_format(item)))
        return items

    def _fetch_insights(self, item):
        """``(status, metrics)`` for one post; never raises."""
        preferred = REEL_METRICS if item['format'] == FORMAT_REEL else FEED_METRICS
        for metrics in (preferred, CORE_METRICS):
            try:
                return INSIGHTS_OK, self.client.get_media_insights(item['id'], metrics)
            except InstagramGraphAPIError as exc:
                if exc.is_permission_error or exc.is_token_error:
                    return INSIGHTS_PERMISSION, {}
                if exc.is_rate_limited:
                    break
                # Unsupported metric for this media, or a post Meta has no insights
                # for (e.g. published before the account went professional): try the
                # core set once, then give up on this post only.
                continue
        logger.info("Instagram insights unavailable for media %s", item.get('id'))
        return INSIGHTS_UNAVAILABLE, {}

    # -- shaping -------------------------------------------------------------

    def _build_row(self, item, followers):
        metrics = item.get('insights') or {}
        has_insights = item.get('insights_status') == INSIGHTS_OK and bool(metrics)
        # Likes/comments fall back to the media node's own counters, which don't need
        # the insights scope.
        likes = metrics.get('likes', item.get('like_count'))
        comments = metrics.get('comments', item.get('comments_count'))
        reach = metrics.get('reach')
        shares = metrics.get('shares')
        saves = metrics.get('saved')
        interactions = metrics.get('total_interactions')
        if interactions is None and has_insights:
            interactions = _sum([likes, comments, shares, saves])
        watch_ms = metrics.get('ig_reels_avg_watch_time')
        fmt = item['format']
        caption = (item.get('caption') or '').strip()
        thumbnail = item.get('thumbnail_url') or (
            item.get('media_url') if fmt != FORMAT_REEL else ''
        )
        return {
            'id': item.get('id'),
            'permalink': item.get('permalink') or '',
            'thumbnail': thumbnail or '',
            # Full-size media for the detail panel: a photo, a carousel's first slide,
            # or a reel's mp4 (Meta omits it for reels with copyrighted audio).
            'media_url': item.get('media_url') or '',
            'media_type': (item.get('media_type') or '').upper(),
            'caption': caption,
            'published_at': item['published_at'],
            'format': fmt,
            'format_label': FORMAT_SINGULAR[fmt],
            'has_insights': has_insights,
            'insights_status': item.get('insights_status'),
            'reach': reach,
            'views': metrics.get('views'),
            'likes': likes,
            'comments': comments,
            'shares': shares,
            'saves': saves,
            'interactions': interactions,
            'follows': metrics.get('follows'),
            'profile_visits': metrics.get('profile_visits'),
            'avg_watch_seconds': round(watch_ms / 1000, 1) if watch_ms else None,
            'engagement_rate': _rate(interactions, reach),
            'reach_rate': _rate(reach, followers),
        }

    def _totals(self, posts, measured, median_reach, followers):
        reach = _sum(p['reach'] for p in measured)
        interactions = _sum(p['interactions'] for p in measured)
        shares = _sum(p['shares'] for p in measured)
        saves = _sum(p['saves'] for p in measured)
        feed_posts = [p for p in measured if p['format'] != FORMAT_REEL]
        follows = _sum(p['follows'] for p in feed_posts)
        avg_reach = _avg(p['reach'] for p in measured)
        return {
            'posts': len(posts),
            'measured_posts': len(measured),
            'reels': sum(1 for p in posts if p['format'] == FORMAT_REEL),
            'posts_per_week': round(len(posts) / (self.window_days / 7), 1),
            'reach': reach,
            'avg_reach': avg_reach,
            'median_reach': round(median_reach) if median_reach is not None else None,
            'reach_rate': _rate(avg_reach, followers),
            # Reach-weighted: total interactions over total reach, so one viral post
            # isn't drowned out by (or doesn't drown out) a dozen small ones.
            'engagement_rate': _rate(interactions, reach),
            'interactions': interactions,
            'shares': shares,
            'saves': saves,
            'saves_shares': _sum([shares, saves]),
            'follows': follows,
            'follows_measured': any(p['follows'] is not None for p in feed_posts),
            'likes': _sum(p['likes'] for p in posts),
            'comments': _sum(p['comments'] for p in posts),
        }

    def _format_breakdown(self, measured, posts):
        rows = []
        for fmt, label in FORMAT_LABELS.items():
            fmt_all = [p for p in posts if p['format'] == fmt]
            if not fmt_all:
                continue
            fmt_measured = [p for p in measured if p['format'] == fmt]
            reach = _sum(p['reach'] for p in fmt_measured)
            interactions = _sum(p['interactions'] for p in fmt_measured)
            rows.append({
                'format': fmt,
                'label': label,
                'posts': len(fmt_all),
                'share_of_posts': _rate(len(fmt_all), len(posts)),
                'avg_reach': _avg(p['reach'] for p in fmt_measured),
                'avg_views': _avg(p['views'] for p in fmt_measured),
                'engagement_rate': _rate(interactions, reach),
                'avg_shares': _avg(p['shares'] for p in fmt_measured),
                'avg_saves': _avg(p['saves'] for p in fmt_measured),
                'avg_likes': _avg(p['likes'] for p in fmt_all),
                'avg_comments': _avg(p['comments'] for p in fmt_all),
            })
        # Flag the leader on each comparative metric, only when there's a choice.
        if len([r for r in rows if r['avg_reach'] is not None]) > 1:
            for metric in ('avg_reach', 'engagement_rate'):
                best = max(
                    (r for r in rows if r[metric] is not None),
                    key=lambda r: r[metric], default=None,
                )
                if best is not None:
                    best[f'best_{metric}'] = True
        return rows

    def _sorted(self, posts):
        if self.sort == 'published':
            return sorted(posts, key=lambda p: p['published_at'], reverse=True)
        field = SORTS[self.sort][1]
        # Posts without the metric sink to the bottom, newest first among themselves.
        with_value = [p for p in posts if p[field] is not None]
        without = [p for p in posts if p[field] is None]
        with_value.sort(key=lambda p: (p[field], p['published_at']), reverse=True)
        without.sort(key=lambda p: p['published_at'], reverse=True)
        return with_value + without

    # -- weekly / monthly roll-up ------------------------------------------------

    def _period_start(self, day):
        """First day of the week (Monday) or month containing ``day``."""
        if self.group == 'week':
            return day - timedelta(days=day.weekday())
        return day.replace(day=1)

    def _next_period(self, start):
        if self.group == 'week':
            return start + timedelta(days=7)
        return (start.replace(day=28) + timedelta(days=4)).replace(day=1)

    def _periods(self, posts, until):
        """Posts rolled up into consecutive weeks/months across the whole window.

        Every period in the window is present, including ones with no posts, so a gap in
        publishing shows up as a gap rather than being silently skipped. Reach and
        interactions sum the posts' own lifetime numbers (not de-duplicated across
        posts), so totals reflect both how often you posted and how well it landed;
        ``avg_reach`` and ``engagement_rate`` are the per-post quality read.
        """
        tz = django_tz.get_current_timezone()
        local_until = until.astimezone(tz).date()
        first_day = local_until - timedelta(days=self.window_days)
        buckets = OrderedDict()
        start = self._period_start(first_day)
        last = self._period_start(local_until)
        while start <= last:
            buckets[start] = []
            start = self._next_period(start)
        for post in posts:
            key = self._period_start(post['published_at'].astimezone(tz).date())
            if key in buckets:
                buckets[key].append(post)

        periods = []
        for start, members in buckets.items():
            measured = [p for p in members if p['has_insights'] and p['reach'] is not None]
            reach = _sum(p['reach'] for p in measured)
            interactions = _sum(p['interactions'] for p in measured)
            end = self._next_period(start) - timedelta(days=1)
            if self.group == 'week':
                label = start.strftime('%b %-d')
                title = f"Week of {start.strftime('%b %-d, %Y')}"
            else:
                label = start.strftime("%b '%y")
                title = start.strftime('%B %Y')
            periods.append({
                'start': start,
                'end': end,
                'label': label,
                'title': title,
                # The first/last period can be cut off by the window edges.
                'partial': start < first_day or end > local_until,
                'posts': len(members),
                'measured_posts': len(measured),
                'reach_by_format': {
                    fmt: _sum(p['reach'] for p in measured if p['format'] == fmt)
                    for fmt in FORMAT_LABELS
                },
                'reach': reach,
                'avg_reach': _avg(p['reach'] for p in measured),
                'interactions': interactions,
                'engagement_rate': _rate(interactions, reach),
                'shares': _sum(p['shares'] for p in measured),
                'saves': _sum(p['saves'] for p in measured),
                'follows': _sum(p['follows'] for p in measured),
            })
        return periods

    def _period_chart(self, periods):
        """Summed reach per week/month, stacked by format."""
        series = []
        for fmt, label in FORMAT_LABELS.items():
            data = [p['reach_by_format'][fmt] for p in periods]
            if any(v is not None for v in data):
                series.append({'format': fmt, 'label': label, 'data': data})
        return {
            'group': self.group,
            'labels': [p['label'] for p in periods] if series else [],
            'meta': [
                {
                    'title': p['title'] + (' (partial)' if p['partial'] else ''),
                    'posts': p['posts'],
                    'avg_reach': p['avg_reach'],
                    'engagement_rate': p['engagement_rate'],
                }
                for p in periods
            ],
            'series': series,
            'median_reach': None,
        }

    def _chart(self, measured, median_reach):
        """Reach per post in publish order, one dataset per format (for colour)."""
        ordered = sorted(
            (p for p in measured if p['reach'] is not None),
            key=lambda p: p['published_at'],
        )
        tz = django_tz.get_current_timezone()
        labels, meta = [], []
        series = {fmt: [] for fmt in FORMAT_LABELS}
        for post in ordered:
            local = post['published_at'].astimezone(tz)
            labels.append(local.strftime('%b %-d'))
            meta.append({
                'date': local.strftime('%a, %b %-d, %Y'),
                'format': post['format_label'],
                'caption': post['caption'][:80],
                'engagement_rate': post['engagement_rate'],
                'views': post['views'],
            })
            for fmt in FORMAT_LABELS:
                series[fmt].append(post['reach'] if post['format'] == fmt else None)
        return {
            'group': 'post',
            'labels': labels,
            'meta': meta,
            'series': [
                {'format': fmt, 'label': FORMAT_LABELS[fmt], 'data': data}
                for fmt, data in series.items() if any(v is not None for v in data)
            ],
            'median_reach': round(median_reach) if median_reach is not None else None,
        }
