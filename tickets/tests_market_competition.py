"""Tests for the Market Competition Agent.

Phase 0 — data-model foundations: the new ``Event.competition_*`` fields, the
``Organization.market_competition_enabled`` flag, and the
``AITokenUsage.FEATURE_MARKET_COMPETITION`` choice.

Phase 1 — the pure, offline core: ``score_competition`` (the deterministic
scoring math) and ``build_queries`` (the fixed search-query plan).

Phase 2 — the web-search client (``search_client``): ``web_search`` (Tavily over
``requests``, cached, fail-silent) and ``search_queries`` (batch + coverage
accounting). All network I/O is mocked, so these stay offline.
"""

from datetime import date, time
from unittest.mock import MagicMock, patch

import requests
from django.core.management import call_command
from django.test import TestCase, override_settings

from .models import AITokenUsage, Event, Organization, Venue
from .services.market_competition import (
    CompetitionResult,
    CompetitorEvent,
    TargetEvent,
    build_queries,
    score_competition,
    search_queries,
    web_search,
)
from .services.market_competition import scoring, search_client

LOCMEM_CACHE = {
    'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'},
}


class Phase0DataModelTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Org MC', slug='org-mc')
        self.venue = Venue.objects.create(
            organization=self.org,
            name='Test Venue',
            city='Test City',
        )
        self.event = Event.objects.create(
            organization=self.org,
            name='Test Event',
            venue=self.venue,
            start_date=date(2024, 6, 15),
            start_time=time(19, 0, 0),
        )

    def test_event_competition_field_defaults(self):
        event = Event.objects.get(pk=self.event.pk)
        self.assertIsNone(event.competition_score)
        self.assertEqual(event.competition_label, '')
        self.assertEqual(event.competition_status, '')
        self.assertEqual(event.competition_input_hash, '')
        self.assertEqual(event.competition_data, {})
        self.assertIsNone(event.competition_generated_at)

    def test_org_market_competition_flag_defaults_false(self):
        org = Organization.objects.get(pk=self.org.pk)
        self.assertFalse(org.market_competition_enabled)

    def test_aitokenusage_feature_choice_present(self):
        self.assertEqual(
            AITokenUsage.FEATURE_MARKET_COMPETITION, 'market_competition'
        )
        self.assertIn(
            (AITokenUsage.FEATURE_MARKET_COMPETITION, 'Market competition'),
            AITokenUsage.FEATURE_CHOICES,
        )
        usage = AITokenUsage.objects.create(
            organization=self.org,
            feature=AITokenUsage.FEATURE_MARKET_COMPETITION,
        )
        self.assertEqual(usage.feature, 'market_competition')


def _competitor(d, *, metro='los angeles', genre='hip-hop', name='Some Show',
                platform='eventbrite'):
    """Build a CompetitorEvent for scoring tests (no DB needed)."""
    return CompetitorEvent(
        name=name,
        date=d,
        venue_name='Rival Venue',
        metro_normalized=metro,
        genre=genre,
        platform=platform,
        source_url='https://example.com/e',
    )


FULL_COVERAGE = {'ratio': 1.0, 'queries_total': 6, 'queries_with_results': 6}


class ScoreCompetitionTests(TestCase):
    """Phase 1 — pure deterministic scorer (`score_competition`)."""

    def setUp(self):
        # Single-day target, confident genre, Los Angeles metro.
        self.target = TargetEvent(
            metro_normalized='Los Angeles',
            start_date=date(2024, 6, 15),
            end_date=None,
            genre_hints=['hip-hop'],
        )

    def _score(self, competitors, undated=None, coverage=None, window_days=3):
        return score_competition(
            self.target, competitors, undated or [],
            coverage or FULL_COVERAGE, window_days=window_days,
        )

    def test_empty_is_zero_low_ready(self):
        result = self._score([])
        self.assertIsInstance(result, CompetitionResult)
        self.assertEqual(result.score, 0)
        self.assertEqual(result.label, 'Low')
        self.assertEqual(result.status, 'ready')  # full coverage => a genuine 0
        self.assertEqual(result.summary, '')

    def test_dense_same_genre_weekend_metro_is_high(self):
        comps = [_competitor(date(2024, 6, 15), name=f'Show {i}') for i in range(4)]
        result = self._score(comps)
        self.assertGreaterEqual(result.score, 60)
        self.assertEqual(result.label, 'High')
        self.assertEqual(result.counts['genre_matched'], 4)

    def test_wrong_metro_excluded(self):
        result = self._score([_competitor(date(2024, 6, 15), metro='new york')])
        self.assertEqual(result.score, 0)
        self.assertEqual(result.counts['same_metro'], 0)

    def test_outside_window_excluded(self):
        # 10 days out, window 3 => filtered.
        result = self._score([_competitor(date(2024, 6, 25))])
        self.assertEqual(result.score, 0)
        self.assertEqual(result.counts['same_metro'], 1)
        self.assertEqual(result.counts['in_window'], 0)

    def test_non_matching_genre_excluded(self):
        result = self._score([_competitor(date(2024, 6, 15), genre='techno')])
        self.assertEqual(result.score, 0)
        self.assertEqual(result.counts['in_window'], 1)
        self.assertEqual(result.counts['genre_matched'], 0)

    def test_date_span_matches_near_either_end(self):
        # Multi-day event (DO5-b): competitors just past either end still match.
        self.target.end_date = date(2024, 6, 18)
        comps = [
            _competitor(date(2024, 6, 13), name='before'),   # 2 days before start
            _competitor(date(2024, 6, 20), name='after'),    # 2 days after end
        ]
        result = self._score(comps)
        self.assertEqual(result.counts['genre_matched'], 2)
        self.assertGreater(result.score, 0)

    def test_no_date_events_excluded_and_returned_in_undated(self):
        # DO5-d: a None-date row (even mixed into `competitors`) is never scored
        # and is surfaced in `undated`.
        comps = [
            _competitor(date(2024, 6, 15), name='dated'),
            _competitor(None, name='undated'),
        ]
        result = self._score(comps)
        self.assertEqual(result.counts['genre_matched'], 1)
        self.assertEqual(len(result.undated), 1)
        self.assertEqual(result.undated[0].name, 'undated')
        self.assertEqual(result.counts['undated'], 1)

    def test_no_confident_genre_fallback_scores_on_density(self):
        # DO5-f: empty hints => drop the genre filter, count city+date density.
        self.target.genre_hints = []
        result = self._score([_competitor(date(2024, 6, 15), genre='techno')])
        self.assertGreater(result.score, 0)
        self.assertEqual(result.counts['genre_matched'], 1)

    def test_low_coverage_zero_is_inconclusive(self):
        # DO2: a near-empty search with a 0 score is not a trustworthy "clear".
        result = self._score([], coverage={'ratio': 0.2})
        self.assertEqual(result.score, 0)
        self.assertEqual(result.status, 'inconclusive')

    def test_adequate_coverage_zero_is_ready(self):
        result = self._score([], coverage={'ratio': 0.8})
        self.assertEqual(result.score, 0)
        self.assertEqual(result.status, 'ready')

    def test_label_band_boundaries(self):
        self.assertEqual(scoring._label_for_score(24), 'Low')
        self.assertEqual(scoring._label_for_score(25), 'Medium')
        self.assertEqual(scoring._label_for_score(59), 'Medium')
        self.assertEqual(scoring._label_for_score(60), 'High')

    def test_counts_and_coverage_shape(self):
        result = self._score([_competitor(date(2024, 6, 15))])
        self.assertEqual(
            set(result.counts),
            {'total', 'same_metro', 'in_window', 'genre_matched', 'undated'},
        )
        self.assertEqual(result.coverage, FULL_COVERAGE)

    def test_result_to_dict_serializes(self):
        result = self._score([_competitor(date(2024, 6, 15))])
        payload = result.to_dict()
        self.assertEqual(payload['label'], result.label)
        self.assertEqual(payload['competitors'][0]['date'], '2024-06-15')
        self.assertEqual(payload['competitors'][0]['metro_normalized'], 'los angeles')


@override_settings(
    MARKET_COMPETITION_PLATFORMS='eventbrite,dice,seetickets',
    MARKET_COMPETITION_DATE_WINDOW_DAYS=3,
)
class BuildQueriesTests(TestCase):
    """Phase 1 — the fixed query plan (`build_queries`, DO3)."""

    def setUp(self):
        self.org = Organization.objects.create(name='Org QP', slug='org-qp')
        self.venue = Venue.objects.create(
            organization=self.org, name='QP Venue', city='Test City',
        )
        self.event = Event.objects.create(
            organization=self.org, name='QP Event', venue=self.venue,
            start_date=date(2024, 6, 15), start_time=time(19, 0, 0),
        )

    def test_exact_query_set(self):
        queries = build_queries(self.event, ['hip-hop', 'rap'])
        self.assertEqual(queries, [
            'hip-hop events in Test City June 12-18, 2024',
            'rap events in Test City June 12-18, 2024',
            'Test City events June 12-18, 2024',
            'eventbrite Test City events June 12-18, 2024',
            'dice Test City events June 12-18, 2024',
            'seetickets Test City events June 12-18, 2024',
        ])

    def test_no_genre_hints_omits_genre_queries(self):
        queries = build_queries(self.event, [])
        self.assertEqual(queries, [
            'Test City events June 12-18, 2024',
            'eventbrite Test City events June 12-18, 2024',
            'dice Test City events June 12-18, 2024',
            'seetickets Test City events June 12-18, 2024',
        ])

    def test_blank_city_yields_no_queries(self):
        self.venue.city = ''
        self.venue.save(update_fields=['city'])
        self.assertEqual(build_queries(self.event, ['hip-hop']), [])

    def test_multi_day_span_widens_date_range(self):
        self.event.end_date = date(2024, 6, 20)
        self.event.save(update_fields=['end_date'])
        queries = build_queries(self.event, [])
        # start-3 = June 12, end+3 = June 23
        self.assertIn('Test City events June 12-23, 2024', queries)


def _tavily_response(results):
    """A mock ``requests.post`` return whose ``.json()`` yields ``results``."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {'results': results}
    return resp


@override_settings(
    CACHES=LOCMEM_CACHE,
    TAVILY_API_KEY='test-key',
    MARKET_COMPETITION_SEARCH_PROVIDER='tavily',
    MARKET_COMPETITION_MAX_RESULTS=25,
)
class WebSearchTests(TestCase):
    """Phase 2 — the Tavily-backed ``web_search`` (all network mocked)."""

    def setUp(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()

    def test_success_parses_results(self):
        payload = [
            {'title': 'Show A', 'url': 'https://e/a', 'content': 'snippet a',
             'score': 0.9},
            {'title': 'Show B', 'url': 'https://e/b', 'content': 'snippet b'},
        ]
        with patch.object(search_client.requests, 'post',
                          return_value=_tavily_response(payload)) as post:
            results = web_search('hip-hop events in LA')
        post.assert_called_once()
        self.assertEqual(results, [
            {'title': 'Show A', 'url': 'https://e/a', 'content': 'snippet a'},
            {'title': 'Show B', 'url': 'https://e/b', 'content': 'snippet b'},
        ])

    def test_timeout_returns_empty_and_warns(self):
        with patch.object(search_client.requests, 'post',
                          side_effect=requests.Timeout('slow')):
            with self.assertLogs('tickets.services.market_competition.search_client',
                                 level='WARNING'):
                results = web_search('q')
        self.assertEqual(results, [])

    def test_http_error_returns_empty_and_warns(self):
        resp = MagicMock()
        resp.raise_for_status.side_effect = requests.HTTPError('500')
        with patch.object(search_client.requests, 'post', return_value=resp):
            with self.assertLogs('tickets.services.market_competition.search_client',
                                 level='WARNING'):
                results = web_search('q')
        self.assertEqual(results, [])

    def test_json_error_returns_empty_and_warns(self):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.side_effect = ValueError('bad json')
        with patch.object(search_client.requests, 'post', return_value=resp):
            with self.assertLogs('tickets.services.market_competition.search_client',
                                 level='WARNING'):
                results = web_search('q')
        self.assertEqual(results, [])

    def test_cache_hit_skips_second_call(self):
        payload = [{'title': 'A', 'url': 'u', 'content': 'c'}]
        with patch.object(search_client.requests, 'post',
                          return_value=_tavily_response(payload)) as post:
            first = web_search('same query')
            second = web_search('same query')
        post.assert_called_once()
        self.assertEqual(first, second)

    def test_failure_sentinel_short_circuits_retry(self):
        with patch.object(search_client.requests, 'post',
                          side_effect=requests.Timeout('slow')) as post:
            first = web_search('flaky query')
            second = web_search('flaky query')
        post.assert_called_once()  # sentinel cached => no second network call
        self.assertEqual(first, [])
        self.assertEqual(second, [])

    @override_settings(TAVILY_API_KEY='')
    def test_missing_key_returns_empty_without_call(self):
        with patch.object(search_client.requests, 'post') as post:
            with self.assertLogs('tickets.services.market_competition.search_client',
                                 level='WARNING'):
                results = web_search('q')
        post.assert_not_called()
        self.assertEqual(results, [])

    @override_settings(TAVILY_API_KEY='')
    def test_missing_key_is_not_cached_as_failure(self):
        # With no key, the empty result must NOT be cached — so dropping the key
        # in later works without a cache-version bump.
        with patch.object(search_client.requests, 'post'):
            web_search('q')
        payload = [{'title': 'A', 'url': 'u', 'content': 'c'}]
        with override_settings(TAVILY_API_KEY='test-key'):
            with patch.object(search_client.requests, 'post',
                              return_value=_tavily_response(payload)) as post:
                results = web_search('q')
        post.assert_called_once()
        self.assertEqual(results, payload)

    @override_settings(MARKET_COMPETITION_MAX_RESULTS=7)
    def test_max_results_from_settings_in_request_body(self):
        with patch.object(search_client.requests, 'post',
                          return_value=_tavily_response([])) as post:
            web_search('q')
        self.assertEqual(post.call_args.kwargs['json']['max_results'], 7)

    def test_max_results_explicit_override(self):
        with patch.object(search_client.requests, 'post',
                          return_value=_tavily_response([])) as post:
            web_search('q', max_results=3)
        self.assertEqual(post.call_args.kwargs['json']['max_results'], 3)

    @override_settings(MARKET_COMPETITION_SEARCH_DEPTH='advanced')
    def test_search_depth_in_request_body(self):
        with patch.object(search_client.requests, 'post',
                          return_value=_tavily_response([])) as post:
            web_search('q')
        self.assertEqual(post.call_args.kwargs['json']['search_depth'], 'advanced')

    def test_domain_filters_passed_through(self):
        with patch.object(search_client.requests, 'post',
                          return_value=_tavily_response([])) as post:
            web_search('q', include_domains=['eventbrite.com'],
                       exclude_domains=['ticketmaster.com'])
        body = post.call_args.kwargs['json']
        self.assertEqual(body['include_domains'], ['eventbrite.com'])
        self.assertEqual(body['exclude_domains'], ['ticketmaster.com'])

    def test_domain_filters_omitted_when_unset(self):
        with patch.object(search_client.requests, 'post',
                          return_value=_tavily_response([])) as post:
            web_search('q')
        body = post.call_args.kwargs['json']
        self.assertNotIn('include_domains', body)
        self.assertNotIn('exclude_domains', body)

    def test_scoped_query_caches_separately_from_unscoped(self):
        payload = [{'title': 'A', 'url': 'u', 'content': 'c'}]
        with patch.object(search_client.requests, 'post',
                          return_value=_tavily_response(payload)) as post:
            web_search('same')
            web_search('same', include_domains=['eventbrite.com'])
        self.assertEqual(post.call_count, 2)  # different cache keys => two calls

    @override_settings(
        MARKET_COMPETITION_INCLUDE_DOMAINS='eventbrite.com, dice.fm',
        MARKET_COMPETITION_EXCLUDE_DOMAINS='ticketmaster.com',
    )
    def test_settings_domains_applied_by_default(self):
        with patch.object(search_client.requests, 'post',
                          return_value=_tavily_response([])) as post:
            web_search('q')
        body = post.call_args.kwargs['json']
        self.assertEqual(body['include_domains'], ['eventbrite.com', 'dice.fm'])
        self.assertEqual(body['exclude_domains'], ['ticketmaster.com'])

    @override_settings(MARKET_COMPETITION_INCLUDE_DOMAINS='eventbrite.com')
    def test_explicit_domains_override_settings(self):
        with patch.object(search_client.requests, 'post',
                          return_value=_tavily_response([])) as post:
            # Explicit [] forces open-web for this call despite the setting.
            web_search('q', include_domains=[])
        self.assertNotIn('include_domains', post.call_args.kwargs['json'])

    @override_settings(MARKET_COMPETITION_SEARCH_PROVIDER='nope')
    def test_unknown_provider_returns_empty(self):
        with patch.object(search_client.requests, 'post') as post:
            with self.assertLogs('tickets.services.market_competition.search_client',
                                 level='WARNING'):
                results = web_search('q')
        post.assert_not_called()
        self.assertEqual(results, [])


@override_settings(CACHES=LOCMEM_CACHE, TAVILY_API_KEY='test-key')
class SearchQueriesCoverageTests(TestCase):
    """Phase 2 — batch runner + coverage accounting (DO2)."""

    def test_coverage_counts_queries_with_results(self):
        def fake_search(query, **kwargs):
            return [{'title': 't'}] if query in ('a', 'c') else []

        with patch.object(search_client, 'web_search', side_effect=fake_search):
            results_by_query, coverage = search_queries(['a', 'b', 'c', 'd'])

        self.assertEqual(set(results_by_query), {'a', 'b', 'c', 'd'})
        self.assertEqual(coverage['queries_total'], 4)
        self.assertEqual(coverage['queries_with_results'], 2)
        self.assertEqual(coverage['ratio'], 0.5)

    def test_all_queries_empty_is_zero_ratio(self):
        with patch.object(search_client, 'web_search', return_value=[]):
            _, coverage = search_queries(['a', 'b'])
        self.assertEqual(coverage['ratio'], 0.0)

    def test_empty_query_list_is_full_ratio(self):
        _, coverage = search_queries([])
        self.assertEqual(coverage['queries_total'], 0)
        self.assertEqual(coverage['ratio'], 1.0)


@override_settings(CACHES=LOCMEM_CACHE, TAVILY_API_KEY='test-key')
class MarketSearchCommandTests(TestCase):
    """Phase 2 — the `market_search` manual-inspection command."""

    def setUp(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()

    def test_prints_results(self):
        from io import StringIO
        payload = [{'title': 'Show A', 'url': 'https://e/a', 'content': 'snippet'}]
        out = StringIO()
        with patch.object(search_client.requests, 'post',
                          return_value=_tavily_response(payload)):
            call_command('market_search', 'some query', stdout=out)
        output = out.getvalue()
        self.assertIn('1 result(s)', output)
        self.assertIn('Show A', output)
        self.assertIn('https://e/a', output)

    @override_settings(TAVILY_API_KEY='')
    def test_no_key_prints_notice_and_no_results(self):
        from io import StringIO
        out = StringIO()
        with patch.object(search_client.requests, 'post') as post:
            call_command('market_search', 'q', stdout=out)
        post.assert_not_called()
        self.assertIn('TAVILY_API_KEY is not set', out.getvalue())
