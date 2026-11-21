"""Tests for the organic Instagram content analytics page.

Covers the Graph client additions (media paging, insights parsing, error flags), the
``InstagramContentInsightsCalculator`` (format classification, metric fallbacks,
permission degradation, totals / format breakdown / sorting / median index), and the
view (feature gate, admin gate, not-connected + error states, rendering).
"""

from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    InstagramConversation, Organization,
    OrganizationMembership, UserProfile,
)
from .services.instagram.content_insights import (
    ALL_METRICS,
    CORE_METRICS,
    FEED_METRICS,
    REEL_METRICS,
    InstagramContentInsightsCalculator,
    classify_format,
)
from .services.instagram.graph_client import (
    InstagramGraphAPIError,
    InstagramGraphClient,
    oauth_scopes,
)


def _ts(days_ago):
    moment = timezone.now() - timedelta(days=days_ago)
    return moment.strftime('%Y-%m-%dT%H:%M:%S+0000')


def _media(media_id, days_ago, media_type='IMAGE', product='FEED', **extra):
    return {
        'id': media_id,
        'caption': f'Caption {media_id}',
        'media_type': media_type,
        'media_product_type': product,
        'media_url': f'https://cdn.example/{media_id}.jpg',
        'permalink': f'https://instagram.com/p/{media_id}',
        'timestamp': _ts(days_ago),
        'like_count': 10,
        'comments_count': 2,
        **extra,
    }


class FakeClient:
    """Stands in for InstagramGraphClient: canned media + insights.

    ``list_media_with_insights`` (the fast path) inlines ``insights`` per item and honours
    the since/until slice, unless ``expansion_error`` is set, which makes every expanded
    request raise so the fallback (``list_media`` + per-post ``get_media_insights``)
    runs. A media id in ``errors`` comes back from the fast path with no insights, and
    raises from ``get_media_insights``.
    """

    def __init__(self, media, insights=None, errors=None, followers=1000,
                 expansion_error=None, ignore_range=False):
        self.media = media
        self.insights = insights or {}
        # media_id -> exception, or callable(metrics) -> exception|None
        self.errors = errors or {}
        self.followers = followers
        self.expansion_error = expansion_error
        # Return every post for every slice (exercises cross-slice de-duplication).
        self.ignore_range = ignore_range
        self.insight_calls = []
        self.expansion_calls = []
        self.list_calls = []
        self.children = {}
        self.children_calls = []
        self.single = {}
        self.single_calls = []

    def _in_range(self, since, until):
        if self.ignore_range:
            return list(self.media)
        out = []
        for item in self.media:
            published = datetime.strptime(item['timestamp'], '%Y-%m-%dT%H:%M:%S%z')
            if (since is None or published >= since) and (until is None or published <= until):
                out.append(item)
        return out

    def list_media_with_insights(self, metrics, since=None, until=None, max_items=1000):
        self.expansion_calls.append((since, until, tuple(metrics)))
        if self.expansion_error is not None:
            raise self.expansion_error
        items = []
        for item in self._in_range(since, until)[:max_items]:
            values = None if item['id'] in self.errors else self.insights.get(item['id'])
            items.append(dict(item, insights=dict(values) if values else None))
        return items

    def get_account_profile(self):
        return {
            'user_id': '1784', 'username': 'cuevenue',
            'followers_count': self.followers, 'media_count': len(self.media),
            'profile_picture_url': '',
        }

    def get_media_with_insights(self, media_id, metrics):
        self.single_calls.append(media_id)
        item = getattr(self, 'single', {}).get(media_id)
        if item is None:
            raise InstagramGraphAPIError('(#100) Object does not exist', code=100)
        values = self.insights.get(media_id)
        return dict(item, insights=dict(values) if values else None)

    def get_media_children(self, media_id):
        self.children_calls.append(media_id)
        if isinstance(self.children, Exception):
            raise self.children
        return self.children.get(media_id, [])

    def list_media(self, since=None, until=None, max_items=100):
        self.list_calls.append((since, until))
        return self._in_range(since, until)[:max_items]

    def get_media_insights(self, media_id, metrics):
        self.insight_calls.append((media_id, tuple(metrics)))
        err = self.errors.get(media_id)
        if callable(err):
            err = err(tuple(metrics))
        if err is not None:
            raise err
        return dict(self.insights.get(media_id, {}))


def _standard_client():
    media = [
        _media('reel1', 2, media_type='VIDEO', product='REELS', thumbnail_url='https://cdn.example/reel1_t.jpg'),
        _media('photo1', 5),
        _media('carousel1', 9, media_type='CAROUSEL_ALBUM'),
        _media('old', 200),  # outside the default 90-day window
    ]
    insights = {
        'reel1': {'reach': 900, 'views': 2000, 'likes': 80, 'comments': 6, 'shares': 30,
                  'saved': 12, 'total_interactions': 128, 'ig_reels_avg_watch_time': 6400},
        'photo1': {'reach': 300, 'views': 500, 'likes': 25, 'comments': 3, 'shares': 2,
                   'saved': 5, 'total_interactions': 35, 'follows': 4, 'profile_visits': 9},
        'carousel1': {'reach': 100, 'views': 150, 'likes': 8, 'comments': 1, 'shares': 0,
                      'saved': 1, 'total_interactions': 10, 'follows': 1},
    }
    return FakeClient(media, insights)


class ClassifyFormatTests(TestCase):
    def test_reels_by_product_type(self):
        self.assertEqual(classify_format({'media_type': 'VIDEO', 'media_product_type': 'REELS'}), 'reel')

    def test_video_without_product_type_is_reel(self):
        self.assertEqual(classify_format({'media_type': 'VIDEO'}), 'reel')

    def test_carousel_and_photo(self):
        self.assertEqual(classify_format({'media_type': 'CAROUSEL_ALBUM'}), 'carousel')
        self.assertEqual(classify_format({'media_type': 'IMAGE'}), 'photo')


class OAuthScopeTests(TestCase):
    def test_insights_only_without_messaging(self):
        self.assertEqual(
            oauth_scopes(include_messaging=False),
            'instagram_business_basic,instagram_business_manage_insights',
        )

    def test_messaging_scope_added_when_agent_on(self):
        self.assertEqual(
            oauth_scopes(include_messaging=True),
            'instagram_business_basic,instagram_business_manage_messages,'
            'instagram_business_manage_insights',
        )


class GraphClientInsightsTests(TestCase):
    def _response(self, payload, status=200):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = payload
        resp.url = 'https://graph.instagram.com/v23.0/x'
        return resp

    @patch('tickets.services.instagram.graph_client.requests.get')
    def test_get_media_insights_parses_values(self, mock_get):
        mock_get.return_value = self._response({'data': [
            {'name': 'reach', 'period': 'lifetime', 'values': [{'value': 120}]},
            {'name': 'saved', 'period': 'lifetime', 'values': [{'value': 4}]},
            {'name': 'views', 'total_value': {'value': 300}},
            {'name': 'broken', 'values': []},
        ]})
        result = InstagramGraphClient('tok').get_media_insights('m1', ['reach', 'saved', 'views'])
        self.assertEqual(result, {'reach': 120, 'saved': 4, 'views': 300})
        self.assertEqual(mock_get.call_args.kwargs['params']['metric'], 'reach,saved,views')

    @patch('tickets.services.instagram.graph_client.requests.get')
    def test_list_media_follows_paging_until_cap(self, mock_get):
        page1 = {'data': [{'id': '1'}, {'id': '2'}], 'paging': {'next': 'https://graph.instagram.com/next1'}}
        page2 = {'data': [{'id': '3'}, {'id': '4'}], 'paging': {'next': 'https://graph.instagram.com/next2'}}
        mock_get.side_effect = [self._response(page1), self._response(page2)]
        items = InstagramGraphClient('tok').list_media(max_items=3)
        self.assertEqual([i['id'] for i in items], ['1', '2', '3'])
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(mock_get.call_args_list[1].args[0], 'https://graph.instagram.com/next1')

    @patch('tickets.services.instagram.graph_client.requests.get')
    def test_list_media_with_insights_inlines_metrics(self, mock_get):
        mock_get.return_value = self._response({'data': [
            {'id': '1', 'insights': {'data': [
                {'name': 'reach', 'values': [{'value': 50}]},
                {'name': 'follows', 'values': [{'value': 2}]},
            ]}},
            {'id': '2'},  # Meta omits insights for some media
        ]})
        since = timezone.now() - timedelta(days=31)
        until = timezone.now()
        items = InstagramGraphClient('tok').list_media_with_insights(
            metrics=['reach', 'follows'], since=since, until=until,
        )
        self.assertEqual(items[0]['insights'], {'reach': 50, 'follows': 2})
        self.assertIsNone(items[1]['insights'])
        params = mock_get.call_args.kwargs['params']
        self.assertIn('insights.metric(reach,follows)', params['fields'])
        self.assertEqual(params['since'], int(since.timestamp()))
        self.assertEqual(params['until'], int(until.timestamp()))

    def test_error_flags(self):
        self.assertTrue(InstagramGraphAPIError('x', code=17).is_rate_limited)
        self.assertTrue(InstagramGraphAPIError('Permissions error', code=200).is_permission_error)
        self.assertTrue(InstagramGraphAPIError('(#10) no permission', code=10).is_permission_error)
        self.assertFalse(
            InstagramGraphAPIError('(#10) Not enough viewers for the media', code=10).is_permission_error
        )
        self.assertFalse(
            InstagramGraphAPIError('sent outside of allowed window', code=10).is_permission_error
        )
        self.assertTrue(InstagramGraphAPIError('expired', code=190).is_token_error)


class ContentInsightsCalculatorTests(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(
            name='IG Content Org', slug='ig-content-org',
            instagram_page_access_token='tok', instagram_business_account_id='1784',
            instagram_username='cuevenue',
        )

    def _calc(self, client, **kwargs):
        return InstagramContentInsightsCalculator(self.org, client=client, **kwargs)

    def test_totals_and_window_filter(self):
        data = self._calc(_standard_client()).calculate()
        t = data['totals']
        self.assertEqual(data['post_count'], 3)
        self.assertEqual(t['reach'], 1300)
        self.assertEqual(t['median_reach'], 300)
        self.assertEqual(t['avg_reach'], 433)
        # Reach-weighted: 173 interactions / 1300 reach.
        self.assertEqual(t['engagement_rate'], 13.3)
        self.assertEqual(t['saves_shares'], 50)
        # Follows only come from feed posts (reels don't report them).
        self.assertEqual(t['follows'], 5)
        self.assertTrue(t['follows_measured'])
        self.assertEqual(t['reach_rate'], 43.3)
        self.assertFalse(data['insights_permission_missing'])

    def test_fast_path_requests_all_metrics_without_per_post_calls(self):
        client = _standard_client()
        self._calc(client).calculate()
        self.assertTrue(client.expansion_calls)
        self.assertEqual(client.expansion_calls[0][2], ALL_METRICS)
        for metric in ('follows', 'profile_visits', 'ig_reels_avg_watch_time', 'reach'):
            self.assertIn(metric, ALL_METRICS)
        self.assertEqual(client.insight_calls, [])

    def test_fallback_requests_metric_set_per_format(self):
        client = _standard_client()
        client.expansion_error = InstagramGraphAPIError('(#100) unsupported', code=100)
        data = self._calc(client).calculate()
        self.assertEqual(data['totals']['reach'], 1300)
        calls = dict(client.insight_calls)
        self.assertEqual(calls['reel1'], REEL_METRICS)
        self.assertEqual(calls['photo1'], FEED_METRICS)
        self.assertEqual(calls['carousel1'], FEED_METRICS)

    def test_reach_index_and_watch_time(self):
        data = self._calc(_standard_client()).calculate()
        rows = {p['id']: p for p in data['posts']}
        self.assertEqual(rows['reel1']['reach_index'], 3.0)
        self.assertEqual(rows['photo1']['reach_index'], 1.0)
        self.assertEqual(rows['reel1']['avg_watch_seconds'], 6.4)
        self.assertEqual(rows['reel1']['thumbnail'], 'https://cdn.example/reel1_t.jpg')
        self.assertEqual(rows['photo1']['thumbnail'], 'https://cdn.example/photo1.jpg')

    def test_format_breakdown_flags_best(self):
        data = self._calc(_standard_client()).calculate()
        formats = {f['format']: f for f in data['formats']}
        self.assertEqual(set(formats), {'reel', 'photo', 'carousel'})
        self.assertTrue(formats['reel'].get('best_avg_reach'))
        self.assertTrue(formats['reel'].get('best_engagement_rate'))
        self.assertFalse(formats['photo'].get('best_avg_reach', False))
        self.assertEqual(formats['reel']['avg_views'], 2000)

    def test_sort_by_reach_and_format_filter(self):
        data = self._calc(_standard_client(), sort='reach').calculate()
        self.assertEqual([p['id'] for p in data['posts']], ['reel1', 'photo1', 'carousel1'])

        data = self._calc(_standard_client(), fmt='photo').calculate()
        self.assertEqual([p['id'] for p in data['posts']], ['photo1'])
        # Totals still describe the whole account, not the filtered view.
        self.assertEqual(data['totals']['posts'], 3)

    def test_default_sort_newest_first(self):
        data = self._calc(_standard_client()).calculate()
        self.assertEqual([p['id'] for p in data['posts']], ['reel1', 'photo1', 'carousel1'])

    def test_unknown_sort_and_window_fall_back(self):
        calc = self._calc(_standard_client(), sort='bogus', window='7y')
        self.assertEqual(calc.sort, 'published')
        self.assertEqual(calc.window, '90d')

    def test_falls_back_to_core_metrics_when_rich_set_rejected(self):
        client = _standard_client()
        unsupported = InstagramGraphAPIError('(#100) metric not supported', code=100)
        client.expansion_error = unsupported
        client.errors['photo1'] = lambda metrics: unsupported if metrics == FEED_METRICS else None
        data = self._calc(client).calculate()
        rows = {p['id']: p for p in data['posts']}
        self.assertTrue(rows['photo1']['has_insights'])
        self.assertIn(('photo1', CORE_METRICS), client.insight_calls)

    def test_post_without_insights_is_kept_and_counted(self):
        client = _standard_client()
        client.errors['carousel1'] = InstagramGraphAPIError('(#100) unavailable', code=100)
        data = self._calc(client).calculate()
        rows = {p['id']: p for p in data['posts']}
        self.assertEqual(data['unavailable_count'], 1)
        self.assertFalse(rows['carousel1']['has_insights'])
        self.assertIsNone(rows['carousel1']['reach'])
        # Media-node counters still show.
        self.assertEqual(rows['carousel1']['likes'], 10)
        self.assertEqual(data['post_count'], 3)
        self.assertEqual(data['totals']['measured_posts'], 2)

    def test_missing_permission_degrades_to_likes_and_comments(self):
        client = _standard_client()
        client.expansion_error = InstagramGraphAPIError(
            '(#10) Application does not have permission', code=10,
        )
        data = self._calc(client).calculate()
        self.assertTrue(data['insights_permission_missing'])
        self.assertIsNone(data['totals']['avg_reach'])
        self.assertIsNone(data['totals']['engagement_rate'])
        self.assertEqual(data['totals']['likes'], 30)
        self.assertEqual(data['chart']['labels'], [])
        # Without the scope per-post calls would fail the same way, so none are made.
        self.assertEqual(client.insight_calls, [])
        self.assertTrue(client.list_calls)

    def test_chart_series_in_publish_order(self):
        data = self._calc(_standard_client()).calculate()
        chart = data['chart']
        self.assertEqual(len(chart['labels']), 3)
        series = {s['format']: s['data'] for s in chart['series']}
        # Oldest first: carousel (9d), photo (5d), reel (2d).
        self.assertEqual(series['carousel'], [100, None, None])
        self.assertEqual(series['reel'], [None, None, 900])
        self.assertEqual(chart['median_reach'], 300)

    def test_chart_follows_format_filter(self):
        # Regression: the chart ignored the format toggle and always plotted every post.
        data = self._calc(_standard_client(), fmt='reel').calculate()
        chart = data['chart']
        self.assertEqual(len(chart['labels']), 1)
        self.assertEqual([s['format'] for s in chart['series']], ['reel'])
        self.assertEqual(chart['series'][0]['data'], [900])
        # The dashed baseline stays the all-posts median, matching the "vs median" column.
        self.assertEqual(chart['median_reach'], 300)

    # -- weekly / monthly grouping ------------------------------------------------
    # Frozen at Fri 2026-11-20 12:00 PT. Posts land on: reel Wed Nov 18 (week of Mon
    # Nov 16), photo Sun Nov 15 + carousel Wed Nov 11 (both week of Mon Nov 9).
    FROZEN_NOW = datetime(2026, 11, 20, 20, 0, tzinfo=dt_timezone.utc)

    def _grouped(self, group, **kwargs):
        with patch('django.utils.timezone.now', return_value=self.FROZEN_NOW):
            return self._calc(_standard_client(), group=group, **kwargs).calculate()

    def test_weekly_periods_cover_window_including_empty_weeks(self):
        data = self._grouped('week')
        periods = data['periods']
        # 90 days back from Fri Nov 20 is Sat Aug 22 -> weeks of Aug 17 .. Nov 16.
        self.assertEqual(len(periods), 14)
        self.assertEqual(periods[0]['start'], date(2026, 8, 17))
        self.assertEqual(periods[-1]['start'], date(2026, 11, 16))
        self.assertTrue(periods[0]['partial'])
        self.assertTrue(periods[-1]['partial'])
        self.assertFalse(periods[-2]['partial'])
        self.assertEqual(periods[0]['posts'], 0)
        self.assertIsNone(periods[0]['reach'])

        this_week, last_week = periods[-1], periods[-2]
        self.assertEqual(this_week['posts'], 1)
        self.assertEqual(this_week['reach'], 900)
        self.assertEqual(this_week['reach_by_format']['reel'], 900)
        self.assertEqual(last_week['posts'], 2)
        self.assertEqual(last_week['reach'], 400)
        self.assertEqual(last_week['avg_reach'], 200)
        self.assertEqual(last_week['interactions'], 45)
        self.assertEqual(last_week['follows'], 5)
        self.assertEqual(sum(p['posts'] for p in periods), 3)

    def test_weekly_chart_is_stacked_by_format(self):
        chart = self._grouped('week')['chart']
        self.assertEqual(chart['group'], 'week')
        self.assertEqual(len(chart['labels']), 14)
        self.assertEqual(chart['labels'][-1], 'Nov 16')
        self.assertIsNone(chart['median_reach'])
        series = {s['format']: s['data'] for s in chart['series']}
        self.assertEqual(series['reel'][-1], 900)
        self.assertEqual(series['photo'][-2], 300)
        self.assertEqual(series['carousel'][-2], 100)
        self.assertIsNone(series['reel'][-2])
        self.assertEqual(chart['meta'][-1]['title'], 'Week of Nov 16, 2026 (partial)')
        self.assertEqual(chart['meta'][-2]['posts'], 2)

    def test_monthly_periods(self):
        data = self._grouped('month')
        periods = data['periods']
        self.assertEqual([p['start'] for p in periods], [
            date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1),
        ])
        self.assertEqual(periods[-1]['label'], "Nov '26")
        self.assertEqual(periods[-1]['title'], 'November 2026')
        self.assertEqual(periods[-1]['posts'], 3)
        self.assertEqual(periods[-1]['reach'], 1300)
        self.assertEqual(data['chart']['labels'], ["Aug '26", "Sep '26", "Oct '26", "Nov '26"])

    def test_grouping_respects_format_filter(self):
        data = self._grouped('week', fmt='photo')
        self.assertEqual(sum(p['posts'] for p in data['periods']), 1)
        self.assertEqual([s['format'] for s in data['chart']['series']], ['photo'])

    def test_post_grouping_has_no_periods_and_unknown_falls_back(self):
        calc = self._calc(_standard_client(), group='fortnight')
        self.assertEqual(calc.group, 'post')
        data = calc.calculate()
        self.assertEqual(data['periods'], [])
        self.assertEqual(data['chart']['group'], 'post')

    def test_top_posts_only_when_enough_to_rank(self):
        data = self._calc(_standard_client()).calculate()
        self.assertEqual(data['top_posts'], [])

        media = [_media(f'p{i}', i + 1) for i in range(6)]
        insights = {f'p{i}': {'reach': (i + 1) * 100, 'total_interactions': 10} for i in range(6)}
        data = self._calc(FakeClient(media, insights)).calculate(refresh=True)
        self.assertEqual([p['id'] for p in data['top_posts']], ['p5', 'p4', 'p3', 'p2'])

    def test_results_cached_until_refresh(self):
        client = _standard_client()
        self._calc(client).calculate()
        first_calls = len(client.expansion_calls)
        self._calc(client, sort='reach').calculate()
        self.assertEqual(len(client.expansion_calls), first_calls)
        self._calc(client).calculate(refresh=True)
        self.assertEqual(len(client.expansion_calls), first_calls * 2)

    def test_one_pull_serves_every_window(self):
        # Regression: each window was fetched (and capped) separately, so switching
        # ranges refetched everything and a long range could silently lose old posts.
        client = _standard_client()
        short = self._calc(client, window='30d').calculate()
        calls = len(client.expansion_calls)
        year = self._calc(client, window='365d').calculate()
        self.assertEqual(len(client.expansion_calls), calls)
        self.assertEqual(short['post_count'], 3)
        self.assertEqual(year['post_count'], 4)
        self.assertIn('old', {p['id'] for p in year['posts']})

    def test_year_is_fetched_as_parallel_weekly_slices(self):
        client = _standard_client()
        with patch('django.utils.timezone.now', return_value=self.FROZEN_NOW):
            self._calc(client).calculate()
        slices = sorted((since, until) for since, until, _m in client.expansion_calls)
        self.assertEqual(len(slices), 53)  # 365 days in 7-day slices
        self.assertEqual(slices[0][0], self.FROZEN_NOW - timedelta(days=365))
        self.assertEqual(slices[-1][1], self.FROZEN_NOW)
        for (_s1, end), (start, _e2) in zip(slices, slices[1:]):
            self.assertEqual(end, start)  # contiguous, no gaps

    def test_posts_returned_by_two_slices_are_counted_once(self):
        media = _standard_client().media
        client = FakeClient(media, _standard_client().insights, ignore_range=True)
        data = self._calc(client, window='365d').calculate()
        self.assertEqual(data['post_count'], 4)
        self.assertEqual(len({p['id'] for p in data['posts']}), 4)

    def test_more_than_150_posts_in_a_year_are_all_kept(self):
        # Regression: a hard 150-post cap cut a ~6 posts/week account off at ~6 months.
        media = [_media(f'p{i}', 1 + i * 1.2) for i in range(280)]
        insights = {f'p{i}': {'reach': 100, 'total_interactions': 5} for i in range(280)}
        data = self._calc(FakeClient(media, insights), window='365d').calculate()
        self.assertEqual(data['post_count'], 280)
        self.assertFalse(data['truncated'])

    def test_cap_flags_only_windows_it_actually_cuts(self):
        with patch('tickets.services.instagram.content_insights.MAX_POSTS', 3):
            data_90 = self._calc(_standard_client(), window='90d').calculate()
            data_365 = self._calc(_standard_client(), window='365d').calculate()
        # The one dropped post (200 days old) is outside 90 days but inside 365.
        self.assertFalse(data_90['truncated'])
        self.assertEqual(data_90['post_count'], 3)
        self.assertTrue(data_365['truncated'])
        self.assertEqual(data_365['post_count'], 3)

    def test_token_error_in_slice_propagates(self):
        client = _standard_client()
        client.expansion_error = InstagramGraphAPIError('expired', code=190)
        with self.assertRaises(InstagramGraphAPIError):
            self._calc(client).calculate()
        self.assertEqual(client.list_calls, [])

    def test_cache_key_changes_with_token(self):
        calc = self._calc(_standard_client())
        before = calc._cache_key()
        self.org.instagram_page_access_token = 'new-token'
        self.assertNotEqual(before, calc._cache_key())

    def test_account_level_error_propagates(self):
        client = _standard_client()
        client.get_account_profile = MagicMock(side_effect=InstagramGraphAPIError('expired', code=190))
        with self.assertRaises(InstagramGraphAPIError):
            self._calc(client).calculate()


class _IGViewBase(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.org = Organization.objects.create(
            name='IG View Org', slug='ig-view-org', instagram_feature_enabled=True,
        )
        self.admin = User.objects.create_user(
            username='igadmin', email='igadmin@example.com', password='testpass123',
        )
        UserProfile.objects.create(user=self.admin, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(user=self.admin, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.host = User.objects.create_user(
            username='ighost', email='ighost@example.com', password='testpass123',
        )
        UserProfile.objects.create(user=self.host, organization=self.org, org_role=UserProfile.OrgRole.HOST)
        OrganizationMembership.objects.create(user=self.host, organization=self.org, org_role=UserProfile.OrgRole.HOST)
        self.url = reverse('tickets:instagram_content_insights')

    def _login(self, email='igadmin@example.com'):
        self.client.login(username=email, password='testpass123')
        self.client.get(reverse('tickets:home'))

    def _connect(self):
        self.org.instagram_page_access_token = 'tok'
        self.org.instagram_business_account_id = '1784'
        self.org.instagram_username = 'cuevenue'
        self.org.save()



class InstagramContentViewTests(_IGViewBase):
    def test_requires_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_404_when_feature_off(self):
        self.org.instagram_feature_enabled = False
        self.org.save(update_fields=['instagram_feature_enabled'])
        self._login()
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_host_cannot_view(self):
        self._login('ighost@example.com')
        self.assertNotEqual(self.client.get(self.url).status_code, 200)

    def test_not_connected_shows_connect_cta(self):
        self._login()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['connected'])
        self.assertContains(response, reverse('tickets:instagram_settings'))

    def test_group_toggle_renders_period_view_and_links_preserve_state(self):
        self._connect()
        self._login()
        with patch(
            'tickets.services.instagram.content_insights.InstagramGraphClient',
            return_value=_standard_client(),
        ):
            response = self.client.get(self.url + '?group=week&format=reel&sort=reach&window=30d')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['active_group'], 'week')
        self.assertContains(response, 'Reach by week')
        self.assertContains(response, 'Avg reach per post')
        links = {key: url for key, _label, url in response.context['group_links']}
        self.assertEqual(links['post'], '?window=30d&format=reel&sort=reach#reach')
        self.assertEqual(links['month'], '?window=30d&format=reel&group=month&sort=reach#reach')
        windows = {key: url for key, _label, url in response.context['window_links']}
        self.assertEqual(windows['90d'], '?format=reel&group=week&sort=reach')
        formats = {key: url for key, _label, url in response.context['format_links']}
        self.assertEqual(formats[''], '?window=30d&group=week&sort=reach')
        self.assertEqual(response.context['refresh_url'],
                         '?window=30d&format=reel&group=week&sort=reach&refresh=1')

    def test_connected_renders_metrics(self):
        self._connect()
        self._login()
        fake = _standard_client()
        with patch(
            'tickets.services.instagram.content_insights.InstagramGraphClient', return_value=fake,
        ):
            response = self.client.get(self.url + '?sort=reach&window=30d')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['active_sort'], 'reach')
        self.assertEqual(response.context['active_window'], '30d')
        self.assertTrue(response.context['has_chart'])
        self.assertContains(response, '@cuevenue')
        self.assertContains(response, 'Reach per post')
        self.assertContains(response, 'igChartData')
        self.assertContains(response, 'Caption reel1')

    def test_permission_banner(self):
        self._connect()
        self._login()
        fake = _standard_client()
        fake.expansion_error = InstagramGraphAPIError('(#10) no permission', code=10)
        with patch(
            'tickets.services.instagram.content_insights.InstagramGraphClient', return_value=fake,
        ):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'need insights access')
        self.assertFalse(response.context['has_chart'])

    def test_api_error_shows_alert(self):
        self._connect()
        self._login()
        fake = MagicMock()
        fake.get_account_profile.side_effect = InstagramGraphAPIError(
            'Instagram API error (code 190): Session expired', code=190,
        )
        with patch(
            'tickets.services.instagram.content_insights.InstagramGraphClient', return_value=fake,
        ):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['token_expired'])
        self.assertContains(response, 'connection has expired')

    def test_analytics_hub_links_page_when_feature_on(self):
        self._login()
        response = self.client.get(reverse('tickets:analytics_overview'))
        self.assertContains(response, self.url)

        self.org.instagram_feature_enabled = False
        self.org.save(update_fields=['instagram_feature_enabled'])
        response = self.client.get(reverse('tickets:analytics_overview'))
        self.assertNotContains(response, self.url)


@override_settings(INSTAGRAM_DM_AGENT_ENABLED=False, INSTAGRAM_APP_ID='app', INSTAGRAM_APP_SECRET='secret')
class DMAgentDisabledTests(_IGViewBase):
    """With the DM agent switched off, Instagram stays usable for content analytics only."""

    def test_dm_agent_pages_404(self):
        self._login()
        conv = InstagramConversation.objects.create(organization=self.org, ig_user_id='cust-1')
        for name, kwargs in [
            ('tickets:instagram_inbox', {}),
            ('tickets:instagram_faq_list', {}),
            ('tickets:instagram_conversation_detail', {'conversation_id': conv.id}),
        ]:
            self.assertEqual(self.client.get(reverse(name, kwargs=kwargs)).status_code, 404, name)
        self.assertEqual(self.client.post(reverse('tickets:instagram_toggle_agent')).status_code, 404)

    def test_settings_and_content_pages_still_available(self):
        self._login()
        response = self.client.get(reverse('tickets:instagram_settings'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Auto-answer')
        self.assertNotContains(response, reverse('tickets:instagram_faq_list'))
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_sidebar_hides_inbox(self):
        self._login()
        response = self.client.get(reverse('tickets:analytics_overview'))
        self.assertNotContains(response, reverse('tickets:instagram_inbox'))

    def test_connect_requests_insights_without_messaging(self):
        self._login()
        response = self.client.post(reverse('tickets:instagram_connect'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('instagram_business_manage_insights', response['Location'])
        self.assertNotIn('instagram_business_manage_messages', response['Location'])

    @patch('tickets.integrations.instagram.exchange_for_long_lived_instagram_token')
    @patch('tickets.integrations.instagram.exchange_instagram_code_for_token')
    @patch('tickets.services.instagram.InstagramGraphClient')
    def test_callback_skips_message_subscription(self, mock_client, mock_code, mock_long):
        self._login()
        session = self.client.session
        session['instagram_oauth_state'] = 'st'
        session.save()
        mock_code.return_value = {'access_token': 'short', 'user_id': '1784'}
        mock_long.return_value = {'access_token': 'long', 'expires_in': 5184000}
        mock_client.return_value.get_me.return_value = {'user_id': '1784', 'username': 'cuevenue'}
        response = self.client.get(reverse('tickets:instagram_callback') + '?state=st&code=abc')
        self.assertEqual(response.status_code, 302)
        mock_client.return_value.subscribe_to_messages.assert_not_called()
        self.org.refresh_from_db()
        self.assertEqual(self.org.instagram_business_account_id, '1784')
        self.assertEqual(self.org.instagram_page_access_token, 'long')

    @override_settings(INSTAGRAM_VALIDATE_WEBHOOKS=False)
    @patch('tickets.tasks.process_instagram_inbound_task.delay')
    def test_webhook_acks_without_processing(self, mock_delay):
        self._connect()
        body = {'object': 'instagram', 'entry': [{'id': '1784', 'messaging': [{
            'sender': {'id': 'cust-1'}, 'recipient': {'id': '1784'},
            'message': {'mid': 'mid-1', 'text': 'When are doors?'},
        }]}]}
        response = self.client.post(
            reverse('tickets:instagram_webhook'), data=body, content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        mock_delay.assert_not_called()

    def test_inbound_task_noops(self):
        from .tasks import process_instagram_inbound_task
        self._connect()
        self.org.instagram_support_agent_enabled = True
        self.org.save(update_fields=['instagram_support_agent_enabled'])
        process_instagram_inbound_task.apply(args=[str(self.org.id), {
            'ig_account_id': '1784', 'sender_id': 'cust-1', 'text': 'hi',
            'provider_message_id': 'mid-1', 'timestamp': 0,
        }])
        self.assertFalse(InstagramConversation.objects.filter(organization=self.org).exists())

    def test_integration_card_describes_content_analytics(self):
        from .integrations.registry import integration_statuses
        card = next(e for e in integration_statuses(self.org) if e['key'] == 'instagram')
        self.assertEqual(card['label'], 'Instagram')


class PostDetailPanelTests(_IGViewBase):
    """The post-detail side panel fragment (fetched when a row/card is clicked)."""

    def setUp(self):
        super().setUp()
        self._connect()
        self.fake = _standard_client()
        self.fake.insights['old'] = {'reach': 500, 'total_interactions': 20}
        self.fake.children = {'carousel1': [
            {'id': 's1', 'media_type': 'IMAGE', 'media_url': 'https://cdn.example/s1.jpg'},
            {'id': 's2', 'media_type': 'VIDEO', 'media_url': 'https://cdn.example/s2.mp4',
             'thumbnail_url': 'https://cdn.example/s2.jpg'},
        ]}

    def _get(self, media_id, query='?window=365d'):
        with patch('tickets.services.instagram.content_insights.InstagramGraphClient', return_value=self.fake):
            return self.client.get(reverse('tickets:instagram_post_detail', args=[media_id]) + query)

    def test_renders_caption_metrics_media_and_benchmarks(self):
        self._login()
        response = self._get('reel1')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Caption reel1')
        self.assertContains(response, '<video src="https://cdn.example/reel1.jpg"', html=False)
        self.assertContains(response, 'Avg watch')
        self.assertNotContains(response, 'Profile visits')
        bench = {b['label']: b for b in response.context['benchmarks']}
        # 365d window: 4 measured posts, so 3 peers with reach 300/100/500 -> median 300.
        self.assertEqual(bench['All posts']['peers'], 3)
        self.assertEqual(bench['All posts']['median_reach'], 300)
        self.assertEqual(bench['All posts']['reach_ratio'], 3.0)
        # Too few same-format peers to compare honestly.
        self.assertEqual(list(bench), ['All posts'])

    def test_carousel_slides_fetched_once_then_cached(self):
        self._login()
        first = self._get('carousel1')
        self.assertEqual(len(first.context['slides']), 2)
        self.assertContains(first, '1 / 2')
        self.assertContains(first, 'poster="https://cdn.example/s2.jpg"')
        self._get('carousel1')
        self.assertEqual(self.fake.children_calls, ['carousel1'])

    def test_carousel_slide_failure_falls_back_to_cover(self):
        self.fake.children = InstagramGraphAPIError('boom', code=100)
        self._login()
        response = self._get('carousel1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['slides'], [])
        self.assertContains(response, 'https://cdn.example/carousel1.jpg')

    def test_post_outside_window_or_unknown_is_404(self):
        self._login()
        self.assertEqual(self._get('old', query='?window=30d').status_code, 404)
        self.assertEqual(self._get('nope').status_code, 404)

    def test_requires_admin_and_connection(self):
        self._login('ighost@example.com')
        self.assertNotEqual(self._get('reel1').status_code, 200)
        self.client.logout()
        self.org.instagram_page_access_token = ''
        self.org.save()
        self._login()
        self.assertEqual(self._get('reel1').status_code, 404)

    def test_page_rows_and_cards_open_the_panel(self):
        self._login()
        with patch('tickets.services.instagram.content_insights.InstagramGraphClient', return_value=self.fake):
            response = self.client.get(self.url + '?window=30d')
        detail_url = reverse('tickets:instagram_post_detail', args=['reel1'])
        self.assertContains(response, 'id="igPostPanel"')
        self.assertContains(response, 'data-window="30d"')
        with patch('tickets.services.instagram.content_insights.InstagramGraphClient', return_value=self.fake):
            response = self.client.get(self.url)
        self.assertContains(response, f'class="ig-post-row" data-detail-url="{detail_url}"')


# ---------------------------------------------------------------------------
# Posts linked to events (event Marketing tab)
# ---------------------------------------------------------------------------

class _EventContentBase(_IGViewBase):
    def setUp(self):
        super().setUp()
        from datetime import date as _date
        from .models import Event, Venue
        self._connect()
        self.venue = Venue.objects.create(organization=self.org, name='Le Poisson Rouge', city='New York')
        self.event = Event.objects.create(
            organization=self.org, name='Familiar Faces NYC', venue=self.venue,
            start_date=timezone.localdate() + timedelta(days=10),
        )
        other_org = Organization.objects.create(name='Other Ev Org', slug='other-ev-org')
        other_venue = Venue.objects.create(organization=other_org, name='Elsewhere', city='Austin')
        self.other_event = Event.objects.create(
            organization=other_org, name='Not yours', venue=other_venue, start_date=timezone.localdate(),
        )
        media = [
            _media('nyc1', 3, media_type='VIDEO', product='REELS', thumbnail_url='https://cdn.example/nyc1.jpg'),
            _media('nyc2', 30),
            _media('la1', 4),
            _media('brand', 2),
            _media('far', 120),
        ]
        captions = {
            'nyc1': 'NYC we are back! tickets in bio',
            'nyc2': 'New York, Le Poisson Rouge is calling',
            'la1': 'LA see you soon',
            'brand': 'familiar faces forever',
            'far': 'NYC throwback',
        }
        for item in media:
            item['caption'] = captions[item['id']]
        insights = {
            'nyc1': {'reach': 1000, 'views': 2500, 'shares': 20, 'saved': 5, 'total_interactions': 100},
            'nyc2': {'reach': 400, 'views': 600, 'shares': 2, 'saved': 3, 'total_interactions': 30,
                     'follows': 4, 'profile_visits': 12},
            'la1': {'reach': 300, 'total_interactions': 10},
            'brand': {'reach': 200, 'total_interactions': 10},
            'far': {'reach': 100, 'total_interactions': 5},
            'ancient': {'reach': 50, 'total_interactions': 1},
        }
        self.fake = FakeClient(media, insights)
        old = _media('ancient', 500)
        old['caption'] = 'from long ago'
        self.fake.single = {'ancient': old}

    def _patched(self):
        return patch('tickets.services.instagram.content_insights.InstagramGraphClient', return_value=self.fake)

    def _link(self, *media_ids):
        from .models import InstagramPostEvent
        for media_id in media_ids:
            InstagramPostEvent.objects.create(organization=self.org, event=self.event, ig_media_id=media_id)


class EventContentCalculatorTests(_EventContentBase):
    def _calc(self):
        from .services.instagram.event_content import EventInstagramContentCalculator
        return EventInstagramContentCalculator(self.org, self.event, client=self.fake).calculate()

    def test_totals_timing_and_older_posts(self):
        self._link('nyc1', 'nyc2', 'ancient', 'deleted')
        data = self._calc()
        self.assertEqual([p['id'] for p in data['posts']], ['ancient', 'nyc2', 'nyc1'])
        t = data['totals']
        self.assertEqual(t['posts'], 3)
        self.assertEqual(t['reach'], 1450)
        self.assertEqual(t['views'], 3100)
        self.assertEqual(t['shares_saves'], 30)
        self.assertEqual(t['engagement_rate'], round(131 / 1450 * 100, 1))
        # Follows/profile visits only come from photos & carousels.
        self.assertEqual(t['follows'], 4)
        self.assertEqual(t['profile_visits'], 12)
        rows = {p['id']: p for p in data['posts']}
        self.assertEqual(rows['nyc1']['timing'], '13 days before')
        self.assertEqual(rows['ancient']['timing'], '510 days before')
        # The deleted post is reported, not silently dropped.
        self.assertEqual(data['missing_count'], 1)
        self.assertEqual(self.fake.single_calls, ['ancient', 'deleted'])

    def test_suggestions_match_city_alias_or_venue_in_window(self):
        data = self._calc()
        suggested = {c['id']: c['reason'] for c in data['suggestions']}
        # nyc1 via the "NYC" alias, nyc2 via city + venue name; LA, brand-only, and
        # out-of-window posts aren't suggested.
        self.assertEqual(set(suggested), {'nyc1', 'nyc2'})
        self.assertIn('Le Poisson Rouge', suggested['nyc2'])
        self.assertEqual(data['suggestions'][0]['id'], 'nyc2')  # matched more terms
        browse = {c['id'] for c in data['browse']}
        self.assertEqual(browse, {'nyc1', 'nyc2', 'la1', 'brand'})

    def test_linked_posts_drop_out_of_suggestions(self):
        self._link('nyc1')
        data = self._calc()
        self.assertNotIn('nyc1', {c['id'] for c in data['suggestions']})
        self.assertNotIn('nyc1', {c['id'] for c in data['browse']})


class PostEventLinkTests(_EventContentBase):
    def _post(self, media_id, event_id, action='link', ajax=True):
        headers = {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'} if ajax else {}
        with self._patched():
            return self.client.post(
                reverse('tickets:instagram_post_event_link', args=[media_id]),
                {'event_id': str(event_id), 'action': action}, **headers,
            )

    def test_link_and_unlink(self):
        from .models import InstagramPostEvent
        self._login()
        response = self._post('nyc1', self.event.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['event']['name'], 'Familiar Faces NYC')
        self._post('nyc1', self.event.pk)  # idempotent
        link = InstagramPostEvent.objects.get(event=self.event, ig_media_id='nyc1')
        self.assertEqual(link.linked_by, self.admin)
        self.assertEqual(self._post('nyc1', self.event.pk, action='unlink').status_code, 200)
        self.assertFalse(InstagramPostEvent.objects.exists())

    def test_a_post_can_link_to_several_events(self):
        from .models import Event, InstagramPostEvent
        second = Event.objects.create(organization=self.org, name='FF LA', venue=self.venue, start_date=timezone.localdate())
        self._login()
        self._post('nyc1', self.event.pk)
        self._post('nyc1', second.pk)
        self.assertEqual(InstagramPostEvent.objects.filter(ig_media_id='nyc1').count(), 2)

    def test_rejects_other_orgs_event_unknown_post_and_hosts(self):
        from .models import InstagramPostEvent
        self._login()
        self.assertEqual(self._post('nyc1', self.other_event.pk).status_code, 404)
        self.assertEqual(self._post('nyc1', 'not-a-uuid').status_code, 404)
        self.assertEqual(self._post('not-mine', self.event.pk).status_code, 404)
        self.client.logout()
        self._login('ighost@example.com')
        self.assertNotEqual(self._post('nyc1', self.event.pk).status_code, 200)
        self.assertFalse(InstagramPostEvent.objects.exists())


class EventInstagramCardTests(_EventContentBase):
    def _get(self):
        with self._patched():
            return self.client.get(reverse('tickets:event_instagram_card', args=[self.event.pk]))

    def test_card_shows_totals_linked_posts_and_suggestions(self):
        self._link('nyc1')
        self._login()
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Total reach')
        self.assertContains(response, '1,000')
        self.assertContains(response, 'NYC we are back!')
        self.assertContains(response, 'Suggested posts')
        self.assertContains(response, 'Mentions New York, Le Poisson Rouge')
        self.assertContains(response, 'data-ig-action="unlink"')

    def test_empty_state(self):
        self._login()
        self.assertContains(self._get(), 'No Instagram posts linked to this event yet')

    def test_other_orgs_event_404_and_feature_gate(self):
        self._login()
        with self._patched():
            other = self.client.get(reverse('tickets:event_instagram_card', args=[self.other_event.pk]))
        self.assertEqual(other.status_code, 404)
        self.org.instagram_feature_enabled = False
        self.org.save(update_fields=['instagram_feature_enabled'])
        self.assertEqual(self._get().status_code, 404)

    def test_token_error_message(self):
        self.fake.get_account_profile = MagicMock(side_effect=InstagramGraphAPIError('expired', code=190))
        self._login()
        self.assertContains(self._get(), 'connection has expired')

    def _event_page(self):
        return self.client.get(reverse('tickets:event_detail', args=[self.event.pk]))

    def test_instagram_has_its_own_marketing_subtab(self):
        self._login()
        response = self._event_page()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-marketing-view="instagram">Instagram</button>')
        self.assertContains(response, 'data-marketing-section="instagram" hidden')
        self.assertContains(response, 'id="eventInstagramCard"')
        self.assertContains(response, reverse('tickets:event_instagram_card', args=[self.event.pk]))
        # Integrations still has its own sub-tab, and Instagram is no longer listed in it.
        self.assertContains(response, 'data-marketing-view="linked">Integrations</button>')
        from .integrations.registry import marketing_providers
        self.assertNotIn('instagram', [p['key'] for p in marketing_providers(self.org)])

    def test_subtab_hidden_without_feature_connection_or_admin(self):
        self._login()
        self.org.instagram_feature_enabled = False
        self.org.save(update_fields=['instagram_feature_enabled'])
        self.assertNotContains(self._event_page(), 'data-marketing-view="instagram"')
        self.org.instagram_feature_enabled = True
        self.org.instagram_page_access_token = ''
        self.org.save(update_fields=['instagram_feature_enabled', 'instagram_page_access_token'])
        self.assertNotContains(self._event_page(), 'data-marketing-view="instagram"')
        self._connect()
        self.client.logout()
        self._login('ighost@example.com')
        response = self._event_page()
        self.assertNotContains(response, 'data-marketing-view="instagram"')
        self.assertNotContains(response, 'id="eventInstagramCard"')


class PostPanelEventsTests(_EventContentBase):
    def test_panel_lists_linked_events_and_nearby_choices(self):
        from .models import Event
        self._link('nyc1')
        far_event = Event.objects.create(organization=self.org, name='Way later', venue=self.venue, start_date=timezone.localdate() + timedelta(days=300))
        self._login()
        with self._patched():
            response = self.client.get(reverse('tickets:instagram_post_detail', args=['nyc1']) + '?window=90d')
        self.assertContains(response, 'Familiar Faces NYC')
        self.assertContains(response, 'js-event-unlink')
        near = [e.pk for e in response.context['near_events']]
        other = [e.pk for e in response.context['other_events']]
        self.assertNotIn(self.event.pk, near + other)  # already linked
        self.assertIn(far_event.pk, other)
        self.assertNotIn(self.other_event.pk, near + other)

    def test_content_table_shows_linked_event(self):
        self._link('nyc1')
        self._login()
        with self._patched():
            response = self.client.get(self.url)
        self.assertContains(response, '<i class="bi bi-calendar-event"></i> Familiar Faces NYC')


class PostPanelStylingTests(TestCase):
    """Regression: the detail panel's CSS lives in instagram_content.html while its
    markup lives in the fetched partial, so a page-CSS edit once deleted every panel
    rule and the panel rendered unstyled. Every panel class must have a rule."""

    def test_every_panel_class_has_a_css_rule(self):
        import re
        from pathlib import Path

        templates = Path(__file__).resolve().parent / 'templates' / 'tickets'
        page = (templates / 'instagram_content.html').read_text()
        partial = (templates / '_instagram_post_detail.html').read_text()
        used = set()
        for attr in re.findall(r'class="([^"]+)"', partial + page):
            used.update(c for c in attr.split() if c.startswith(('igd-', 'ig-panel')))
        self.assertIn('igd-metrics', used)
        missing = sorted(c for c in used if not re.search(r'\.' + re.escape(c) + r'[\s{,.:\[]', page))
        self.assertEqual(missing, [])
