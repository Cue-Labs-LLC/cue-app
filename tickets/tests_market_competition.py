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

from datetime import date, time, timedelta
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    AITokenUsage,
    Event,
    EventTalent,
    Organization,
    UserProfile,
    Venue,
)
from .services.market_competition import (
    CompetitionResult,
    CompetitorEvent,
    TargetEvent,
    build_queries,
    calculate_event_competition,
    derive_genre_hints,
    scan_competitors,
    score_competition,
    search_queries,
    web_search,
)
from .services.market_competition import calculator, scanner, scoring, search_client
from .services.market_competition.scanner import ExtractedEvent, ExtractedEvents, ScanResult

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
        # With genres provided, EVERY query carries the genre(s) and the event's
        # own date: per-genre queries plus genre-scoped platform queries, and no
        # bare city-density query.
        queries = build_queries(self.event, ['hip-hop', 'rap'])
        self.assertEqual(queries, [
            'hip-hop events in Test City June 15, 2024',
            'rap events in Test City June 15, 2024',
            'eventbrite hip-hop rap events in Test City June 15, 2024',
            'dice hip-hop rap events in Test City June 15, 2024',
            'seetickets hip-hop rap events in Test City June 15, 2024',
        ])

    def test_single_genre_scopes_every_query(self):
        queries = build_queries(self.event, ['R&B'])
        self.assertEqual(queries, [
            'R&B events in Test City June 15, 2024',
            'eventbrite R&B events in Test City June 15, 2024',
            'dice R&B events in Test City June 15, 2024',
            'seetickets R&B events in Test City June 15, 2024',
        ])

    def test_no_genre_hints_omits_genre_queries(self):
        queries = build_queries(self.event, [])
        self.assertEqual(queries, [
            'Test City events June 15, 2024',
            'eventbrite Test City events June 15, 2024',
            'dice Test City events June 15, 2024',
            'seetickets Test City events June 15, 2024',
        ])

    def test_every_query_includes_the_event_date(self):
        for q in build_queries(self.event, ['R&B']):
            self.assertIn('June 15, 2024', q)

    def test_blank_city_yields_no_queries(self):
        self.venue.city = ''
        self.venue.save(update_fields=['city'])
        self.assertEqual(build_queries(self.event, ['hip-hop']), [])

    def test_multi_day_span_uses_full_span(self):
        self.event.end_date = date(2024, 6, 20)
        self.event.save(update_fields=['end_date'])
        queries = build_queries(self.event, [])
        # The event's own span (not window-widened): June 15-20.
        self.assertIn('Test City events June 15-20, 2024', queries)


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

    def test_success_logs_the_query(self):
        payload = [{'title': 'Show A', 'url': 'https://e/a', 'content': 'snippet a'}]
        with patch.object(search_client.requests, 'post',
                          return_value=_tavily_response(payload)):
            with self.assertLogs('tickets.services.market_competition.search_client',
                                 level='INFO') as cm:
                web_search('hip-hop events in LA')
        self.assertTrue(any("query='hip-hop events in LA'" in m for m in cm.output))
        self.assertTrue(any('1 result(s)' in m for m in cm.output))

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

    def test_forwards_domains_to_web_search(self):
        with patch.object(search_client, 'web_search', return_value=[]) as ws:
            search_queries(['q1'], include_domains=['eventbrite.com'],
                           exclude_domains=['ticketmaster.com'])
        _, kwargs = ws.call_args
        self.assertEqual(kwargs.get('include_domains'), ['eventbrite.com'])
        self.assertEqual(kwargs.get('exclude_domains'), ['ticketmaster.com'])


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


# --- Phase 3 — scanner + calculator (LLM mocked) -------------------------------

def _extract_llm(parsed, *, extract_tokens=(10, 5, 15),
                 narrative='The weekend looks crowded; consider a different date.',
                 narrative_tokens=(8, 4, 12), parsing_error=None):
    """A MagicMock ChatOpenAI usable for BOTH the extraction and narrative calls.

    ``with_structured_output(...).invoke`` returns the ``include_raw`` envelope
    (extraction); ``.invoke`` returns a narrative response with ``.content``.
    """
    raw = MagicMock()
    raw.usage_metadata = {
        'input_tokens': extract_tokens[0],
        'output_tokens': extract_tokens[1],
        'total_tokens': extract_tokens[2],
    }
    structured = MagicMock()
    structured.invoke.return_value = {
        'raw': raw, 'parsed': parsed, 'parsing_error': parsing_error,
    }

    narr = MagicMock()
    narr.content = narrative
    narr.usage_metadata = {
        'input_tokens': narrative_tokens[0],
        'output_tokens': narrative_tokens[1],
        'total_tokens': narrative_tokens[2],
    }

    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    llm.invoke.return_value = narr
    return llm


def _results_by_query(n=6):
    """A canned (results_by_query, coverage) pair, as `search_queries` would return."""
    results = [{'title': 'A show', 'url': 'https://e/a', 'content': 'some event listing'}]
    rbq = {f'q{i}': results for i in range(n)}
    coverage = {'queries_total': n, 'queries_with_results': n, 'ratio': 1.0}
    return rbq, coverage


@override_settings(TAVILY_API_KEY='test-key', OPENAI_API_KEY='test-key',
                   OPENAI_MODEL='gpt-4o', MARKET_COMPETITION_DATE_WINDOW_DAYS=3)
class DeriveGenreHintsTests(TestCase):
    """D1 — the deterministic keyword-map genre derivation (pure, no LLM)."""

    def setUp(self):
        self.org = Organization.objects.create(name='Org G', slug='org-g')
        self.venue = Venue.objects.create(organization=self.org, name='V', city='LA')

    def _event(self, name='Show', description=''):
        return Event.objects.create(
            organization=self.org, venue=self.venue, name=name,
            description=description, start_date=date(2026, 6, 15),
        )

    def test_matches_from_name(self):
        event = self._event(name='Techno Warehouse Party')
        self.assertEqual(derive_genre_hints(event), ['techno'])

    def test_matches_from_description(self):
        event = self._event(name='Friday Night', description='A night of hip-hop and rap.')
        self.assertIn('hip-hop', derive_genre_hints(event))

    def test_matches_from_talent_lineup(self):
        event = self._event(name='Live at the Hall')
        EventTalent.objects.create(event=event, name='DJ Comedy Standup', order=0)
        self.assertIn('comedy', derive_genre_hints(event))

    def test_no_match_returns_empty(self):
        event = self._event(name='Annual Gala', description='An evening affair.')
        self.assertEqual(derive_genre_hints(event), [])

    def test_hints_are_deduped_and_stable(self):
        event = self._event(name='Hip-Hop & Rap Night', description='rap trap hip hop')
        self.assertEqual(derive_genre_hints(event), ['hip-hop'])


@override_settings(TAVILY_API_KEY='test-key', OPENAI_API_KEY='test-key',
                   OPENAI_MODEL='gpt-4o', MARKET_COMPETITION_DATE_WINDOW_DAYS=3,
                   MARKET_COMPETITION_PLATFORMS='eventbrite,dice,seetickets')
class ScanCompetitorsTests(TestCase):
    """P3 — the LLM scanner: extraction, normalization, dedupe, self-exclusion."""

    def setUp(self):
        self.org = Organization.objects.create(name='Org S', slug='org-s')
        self.venue = Venue.objects.create(
            organization=self.org, name='Home Venue', city='Los Angeles', state='CA',
        )
        self.event = Event.objects.create(
            organization=self.org, venue=self.venue, name='Target Show',
            start_date=date(2026, 6, 15), end_date=date(2026, 6, 15),
        )

    def _extracted(self, *events, target_metro='Los Angeles, CA'):
        return ExtractedEvents(target_metro_normalized=target_metro, events=list(events))

    def test_returns_normalized_rows(self):
        parsed = self._extracted(
            ExtractedEvent(name='Rival A', date='2026-06-16', venue_name='Club X',
                           metro_normalized='Los Angeles, CA', genre='techno',
                           platform='dice', source_url='https://dice/a'),
            ExtractedEvent(name='Rival B', date='2026-06-14', venue_name='Hall Y',
                           metro_normalized='Los Angeles, CA', genre='house',
                           platform='eventbrite', source_url='https://eb/b'),
        )
        with patch.object(scanner, 'search_queries', return_value=_results_by_query()), \
                patch('langchain_openai.ChatOpenAI', return_value=_extract_llm(parsed)):
            result = scan_competitors(self.org, self.event, ['techno'])
        self.assertTrue(result.ok)
        self.assertEqual(len(result.competitors), 2)
        a = result.competitors[0]
        self.assertEqual(a.name, 'Rival A')
        self.assertEqual(a.date, date(2026, 6, 16))
        self.assertEqual(a.metro_normalized, 'los angeles, ca')  # lowercased

    def test_target_metro_normalized_emitted(self):
        parsed = self._extracted(target_metro='Los Angeles, CA')
        with patch.object(scanner, 'search_queries', return_value=_results_by_query()), \
                patch('langchain_openai.ChatOpenAI', return_value=_extract_llm(parsed)):
            result = scan_competitors(self.org, self.event, ['techno'])
        self.assertEqual(result.target_metro_normalized, 'los angeles, ca')  # DO5-e

    def test_dedupe_across_platforms(self):
        # Same show on three sites => one row (DO5-c).
        same = dict(name='Big Rival', date='2026-06-16', venue_name='Club X',
                    metro_normalized='Los Angeles, CA', genre='techno')
        parsed = self._extracted(
            ExtractedEvent(platform='eventbrite', source_url='https://eb/x', **same),
            ExtractedEvent(platform='dice', source_url='https://dice/x', **same),
            ExtractedEvent(platform='seetickets', source_url='https://st/x', **same),
        )
        with patch.object(scanner, 'search_queries', return_value=_results_by_query()), \
                patch('langchain_openai.ChatOpenAI', return_value=_extract_llm(parsed)):
            result = scan_competitors(self.org, self.event, ['techno'])
        self.assertEqual(len(result.competitors), 1)

    def test_self_exclusion(self):
        # The LLM echoing the target back must never become its own competitor (DO5-a).
        parsed = self._extracted(
            ExtractedEvent(name='Target Show', date='2026-06-15', venue_name='Home Venue',
                           metro_normalized='Los Angeles, CA', genre='techno',
                           platform='eventbrite', source_url='https://eb/self'),
            ExtractedEvent(name='Rival A', date='2026-06-16', venue_name='Club X',
                           metro_normalized='Los Angeles, CA', genre='techno',
                           platform='dice', source_url='https://dice/a'),
        )
        with patch.object(scanner, 'search_queries', return_value=_results_by_query()), \
                patch('langchain_openai.ChatOpenAI', return_value=_extract_llm(parsed)):
            result = scan_competitors(self.org, self.event, ['techno'])
        names = [c.name for c in result.competitors]
        self.assertEqual(names, ['Rival A'])

    def test_blank_date_becomes_none(self):
        # DO5-d — an unconfident date is None, not a guess.
        parsed = self._extracted(
            ExtractedEvent(name='Undated Rival', date=None, venue_name='Club Z',
                           metro_normalized='Los Angeles, CA', genre='techno',
                           platform='dice', source_url='https://dice/z'),
        )
        with patch.object(scanner, 'search_queries', return_value=_results_by_query()), \
                patch('langchain_openai.ChatOpenAI', return_value=_extract_llm(parsed)):
            result = scan_competitors(self.org, self.event, ['techno'])
        self.assertIsNone(result.competitors[0].date)

    def test_extraction_exception_yields_not_ok(self):
        llm = MagicMock()
        llm.with_structured_output.return_value.invoke.side_effect = RuntimeError('boom')
        with patch.object(scanner, 'search_queries', return_value=_results_by_query()), \
                patch('langchain_openai.ChatOpenAI', return_value=llm):
            result = scan_competitors(self.org, self.event, ['techno'])
        self.assertFalse(result.ok)
        self.assertEqual(result.competitors, [])

    def test_parsing_error_yields_not_ok(self):
        parsed = self._extracted()
        llm = _extract_llm(parsed, parsing_error='bad')
        with patch.object(scanner, 'search_queries', return_value=_results_by_query()), \
                patch('langchain_openai.ChatOpenAI', return_value=llm):
            result = scan_competitors(self.org, self.event, ['techno'])
        self.assertFalse(result.ok)

    def test_meters_extract_stage(self):
        parsed = self._extracted(
            ExtractedEvent(name='Rival A', date='2026-06-16', venue_name='Club X',
                           metro_normalized='Los Angeles, CA', genre='techno',
                           platform='dice', source_url='https://dice/a'),
        )
        with patch.object(scanner, 'search_queries', return_value=_results_by_query()), \
                patch('langchain_openai.ChatOpenAI', return_value=_extract_llm(parsed)):
            scan_competitors(self.org, self.event, ['techno'])
        usage = AITokenUsage.objects.get(organization=self.org)
        self.assertEqual(usage.feature, AITokenUsage.FEATURE_MARKET_COMPETITION)
        self.assertEqual(usage.metadata.get('stage'), 'extract')
        self.assertEqual(usage.total_tokens, 15)

    def test_no_city_skips_llm(self):
        self.venue.city = ''
        self.venue.save()
        with patch('langchain_openai.ChatOpenAI') as mock_llm:
            result = scan_competitors(self.org, self.event, ['techno'])
        mock_llm.assert_not_called()
        self.assertTrue(result.ok)
        self.assertEqual(result.competitors, [])

    def test_interleaving_keeps_late_query_results_within_cap(self):
        # A sequential fill would drain the first query (which alone overflows the
        # MAX_SNIPPETS cap) and starve the later one. Round-robin must keep the
        # late query's result in the payload the LLM sees.
        from tickets.services.market_competition import scanner as scanner_mod
        big = [{'title': f'A{i}', 'url': f'https://a/{i}', 'content': 'x'}
               for i in range(scanner_mod.MAX_SNIPPETS + 20)]
        late = [{'title': 'LATE', 'url': 'https://late/1', 'content': 'the rival'}]
        results_by_query = {'q_first': big, 'q_last': late}

        captured = {}

        def _fake_structured(schema, include_raw=False):
            m = MagicMock()

            def _invoke(messages, **kwargs):
                captured['user'] = messages[1]['content']
                raw = MagicMock()
                raw.usage_metadata = {'input_tokens': 1, 'output_tokens': 1,
                                      'total_tokens': 2}
                return {'raw': raw, 'parsed': ExtractedEvents(), 'parsing_error': None}

            m.invoke.side_effect = _invoke
            return m

        llm = MagicMock()
        llm.with_structured_output.side_effect = _fake_structured
        with patch.object(scanner, 'search_queries',
                          return_value=(results_by_query,
                                        {'queries_total': 2, 'queries_with_results': 2,
                                         'ratio': 1.0})), \
                patch('langchain_openai.ChatOpenAI', return_value=llm):
            scan_competitors(self.org, self.event, ['techno'])
        self.assertIn('https://late/1', captured['user'])

    def test_domains_forwarded_to_search(self):
        parsed = self._extracted()
        with patch.object(scanner, 'search_queries',
                          return_value=_results_by_query()) as sq, \
                patch('langchain_openai.ChatOpenAI', return_value=_extract_llm(parsed)):
            scan_competitors(self.org, self.event, ['techno'],
                             include_domains=['eventbrite.com'],
                             exclude_domains=['ticketmaster.com'])
        _, kwargs = sq.call_args
        self.assertEqual(kwargs.get('include_domains'), ['eventbrite.com'])
        self.assertEqual(kwargs.get('exclude_domains'), ['ticketmaster.com'])


@override_settings(TAVILY_API_KEY='test-key', OPENAI_API_KEY='test-key',
                   OPENAI_MODEL='gpt-4o', MARKET_COMPETITION_DATE_WINDOW_DAYS=3,
                   MARKET_COMPETITION_PLATFORMS='eventbrite,dice,seetickets',
                   MARKET_COMPETITION_SOURCES='web')
class CalculateCompetitionTests(TestCase):
    """P3 — the calculator orchestration end to end (search + LLM mocked)."""

    def setUp(self):
        self.org = Organization.objects.create(name='Org C', slug='org-c')
        self.venue = Venue.objects.create(
            organization=self.org, name='Home Venue', city='Los Angeles', state='CA',
        )
        self.event = Event.objects.create(
            organization=self.org, venue=self.venue, name='Techno Warehouse Party',
            start_date=date(2026, 6, 15), end_date=date(2026, 6, 15),
        )

    def _parsed(self):
        return ExtractedEvents(
            target_metro_normalized='Los Angeles, CA',
            events=[
                ExtractedEvent(name='Rival Techno Night', date='2026-06-16',
                               venue_name='Club X', metro_normalized='Los Angeles, CA',
                               genre='techno', platform='dice', source_url='https://dice/x'),
            ],
        )

    def test_happy_path_returns_ready_result(self):
        with patch.object(scanner, 'search_queries', return_value=_results_by_query()), \
                patch('langchain_openai.ChatOpenAI', return_value=_extract_llm(self._parsed())):
            result = calculate_event_competition(self.org, self.event)
        self.assertEqual(result.status, 'ready')
        self.assertEqual(len(result.competitors), 1)
        self.assertGreater(result.score, 0)
        self.assertTrue(result.summary)

    def test_meters_both_stages(self):
        with patch.object(scanner, 'search_queries', return_value=_results_by_query()), \
                patch('langchain_openai.ChatOpenAI', return_value=_extract_llm(self._parsed())):
            calculate_event_competition(self.org, self.event)
        stages = sorted(
            u.metadata.get('stage')
            for u in AITokenUsage.objects.filter(
                organization=self.org,
                feature=AITokenUsage.FEATURE_MARKET_COMPETITION,
            )
        )
        self.assertEqual(stages, ['extract', 'narrative'])

    def test_unavailable_when_no_city(self):
        self.venue.city = ''
        self.venue.save()
        with patch('langchain_openai.ChatOpenAI') as mock_llm:
            result = calculate_event_competition(self.org, self.event)
        mock_llm.assert_not_called()
        self.assertEqual(result.status, 'unavailable')

    @override_settings(TAVILY_API_KEY='')
    def test_unavailable_when_key_unset(self):
        with patch.object(scanner, 'scan_competitors') as mock_scan:
            result = calculate_event_competition(self.org, self.event)
        mock_scan.assert_not_called()
        self.assertEqual(result.status, 'unavailable')

    def test_unavailable_when_all_searches_empty(self):
        empty = ScanResult(competitors=[], target_metro_normalized='',
                           coverage={'queries_total': 6, 'queries_with_results': 0,
                                     'ratio': 0.0}, ok=True)
        with patch.object(scanner, 'scan_competitors', return_value=empty):
            result = calculate_event_competition(self.org, self.event)
        self.assertEqual(result.status, 'unavailable')

    def test_unavailable_when_extraction_failed(self):
        failed = ScanResult(competitors=[], target_metro_normalized='',
                            coverage={'queries_total': 6, 'queries_with_results': 6,
                                      'ratio': 1.0}, ok=False)
        with patch.object(scanner, 'scan_competitors', return_value=failed):
            result = calculate_event_competition(self.org, self.event)
        self.assertEqual(result.status, 'unavailable')

    def test_explicit_genre_hints_override_derivation(self):
        # The event name derives ['techno']; passing ['jazz'] must override that so
        # only the jazz competitor matches (proves the override reaches the scorer).
        good = ScanResult(
            competitors=[
                CompetitorEvent(name='Techno Night', date=date(2026, 6, 16),
                                venue_name='X', metro_normalized='los angeles, ca',
                                genre='techno', platform='dice', source_url='u1'),
                CompetitorEvent(name='Jazz Eve', date=date(2026, 6, 16),
                                venue_name='Y', metro_normalized='los angeles, ca',
                                genre='jazz', platform='dice', source_url='u2'),
            ],
            target_metro_normalized='los angeles, ca',
            coverage={'queries_total': 6, 'queries_with_results': 6, 'ratio': 1.0},
            ok=True,
        )
        narr = MagicMock(content='n',
                         usage_metadata={'input_tokens': 1, 'output_tokens': 1,
                                         'total_tokens': 2})
        llm = MagicMock()
        llm.invoke.return_value = narr
        with patch.object(scanner, 'scan_competitors', return_value=good), \
                patch('langchain_openai.ChatOpenAI', return_value=llm):
            result = calculate_event_competition(
                self.org, self.event, genre_hints=['jazz'])
        self.assertEqual([c.name for c in result.competitors], ['Jazz Eve'])

    def test_narrative_failure_keeps_score(self):
        # D6 — narrative LLM failure must not drop the score/label/status.
        good = ScanResult(
            competitors=[CompetitorEvent(
                name='Rival Techno Night', date=date(2026, 6, 16), venue_name='Club X',
                metro_normalized='los angeles, ca', genre='techno', platform='dice',
                source_url='https://dice/x')],
            target_metro_normalized='los angeles, ca',
            coverage={'queries_total': 6, 'queries_with_results': 6, 'ratio': 1.0},
            ok=True,
        )
        llm = MagicMock()
        llm.invoke.side_effect = RuntimeError('narrative down')
        with patch.object(scanner, 'scan_competitors', return_value=good), \
                patch('langchain_openai.ChatOpenAI', return_value=llm):
            result = calculate_event_competition(self.org, self.event)
        self.assertEqual(result.status, 'ready')
        self.assertGreater(result.score, 0)
        self.assertEqual(result.summary, '')


@override_settings(TAVILY_API_KEY='test-key', OPENAI_API_KEY='test-key',
                   OPENAI_MODEL='gpt-4o', MARKET_COMPETITION_DATE_WINDOW_DAYS=3,
                   MARKET_COMPETITION_PLATFORMS='eventbrite,dice,seetickets',
                   MARKET_COMPETITION_SOURCES='web')
class ScanEventCompetitionCommandTests(TestCase):
    """P3 — the `scan_event_competition` management command (demo surface)."""

    def setUp(self):
        self.org = Organization.objects.create(name='Org CMD', slug='org-cmd')
        self.venue = Venue.objects.create(
            organization=self.org, name='Home Venue', city='Los Angeles', state='CA',
        )
        self.event = Event.objects.create(
            organization=self.org, venue=self.venue, name='Techno Warehouse Party',
            start_date=date(2026, 6, 15), end_date=date(2026, 6, 15),
        )

    def test_prints_report(self):
        from io import StringIO
        parsed = ExtractedEvents(
            target_metro_normalized='Los Angeles, CA',
            events=[ExtractedEvent(name='Rival Techno Night', date='2026-06-16',
                                   venue_name='Club X', metro_normalized='Los Angeles, CA',
                                   genre='techno', platform='dice',
                                   source_url='https://dice/x')],
        )
        out = StringIO()
        with patch.object(scanner, 'search_queries', return_value=_results_by_query()), \
                patch('langchain_openai.ChatOpenAI', return_value=_extract_llm(parsed)):
            call_command('scan_event_competition', str(self.event.id), stdout=out)
        output = out.getvalue()
        self.assertIn('status:', output)
        self.assertIn('Rival Techno Night', output)

    @override_settings(TAVILY_API_KEY='')
    def test_no_key_prints_notice(self):
        from io import StringIO
        out = StringIO()
        call_command('scan_event_competition', str(self.event.id), stdout=out)
        self.assertIn('TAVILY_API_KEY is not set', out.getvalue())

    def test_bad_event_id_raises_command_error(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('scan_event_competition', 'not-a-real-id')

    def test_genres_flag_overrides_and_is_reported(self):
        from io import StringIO
        parsed = ExtractedEvents(
            target_metro_normalized='Los Angeles, CA',
            events=[ExtractedEvent(name='Jazz Eve', date='2026-06-16',
                                   venue_name='Y', metro_normalized='Los Angeles, CA',
                                   genre='jazz', platform='dice',
                                   source_url='https://dice/y')],
        )
        out = StringIO()
        with patch.object(scanner, 'search_queries', return_value=_results_by_query()), \
                patch('langchain_openai.ChatOpenAI', return_value=_extract_llm(parsed)):
            call_command('scan_event_competition', str(self.event.id),
                         '--genres', 'jazz,soul', stdout=out)
        output = out.getvalue()
        self.assertIn('genres:   jazz, soul', output)
        self.assertIn('Jazz Eve', output)

    def test_domain_flags_forwarded_and_reported(self):
        from io import StringIO
        parsed = ExtractedEvents(target_metro_normalized='Los Angeles, CA', events=[])
        out = StringIO()
        with patch.object(scanner, 'search_queries',
                          return_value=_results_by_query()) as sq, \
                patch('langchain_openai.ChatOpenAI', return_value=_extract_llm(parsed)):
            call_command('scan_event_competition', str(self.event.id),
                         '--include-domains', 'eventbrite.com,dice.fm',
                         '--exclude-domains', 'ticketmaster.com', stdout=out)
        _, kwargs = sq.call_args
        self.assertEqual(kwargs.get('include_domains'), ['eventbrite.com', 'dice.fm'])
        self.assertEqual(kwargs.get('exclude_domains'), ['ticketmaster.com'])
        output = out.getvalue()
        self.assertIn('include:  eventbrite.com, dice.fm', output)
        self.assertIn('exclude:  ticketmaster.com', output)


# --- Phase 4 — Celery task + persistence + gates (calculate mocked) ------------

def _result(status='ready', score=40, label='Medium', summary='Crowded weekend.'):
    """A canned CompetitionResult, as the calculator would return."""
    comp = CompetitorEvent(
        name='Rival Techno Night', date=date(2026, 6, 16), venue_name='Club X',
        metro_normalized='los angeles, ca', genre='techno', platform='dice',
        source_url='https://dice/x',
    )
    return CompetitionResult(
        score=score, label=label, status=status,
        competitors=[comp], undated=[],
        counts={'total': 1, 'same_metro': 1, 'in_window': 1,
                'genre_matched': 1, 'undated': 0},
        coverage={'queries_total': 6, 'queries_with_results': 6, 'ratio': 1.0},
        summary=summary,
    )


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=False,
    CACHES=LOCMEM_CACHE,
    TAVILY_API_KEY='test-key',
    MARKET_COMPETITION_RESULT_TTL_DAYS=7,
    MARKET_COMPETITION_DAILY_SCAN_CAP=50,
    MARKET_COMPETITION_DATE_WINDOW_DAYS=3,
)
class Phase4TaskTests(TestCase):
    """P4 — the async scan task, persistence, freshness/TTL, daily cap, lock."""

    CALC = 'tickets.services.market_competition.calculate_event_competition'

    def setUp(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()  # LocMem persists across tests in-process.
        self.org = Organization.objects.create(name='Org P4', slug='org-p4')
        self.venue = Venue.objects.create(
            organization=self.org, name='Home Venue', city='Los Angeles', state='CA',
        )
        self.event = Event.objects.create(
            organization=self.org, venue=self.venue, name='Techno Warehouse Party',
            start_date=date(2026, 6, 15), end_date=date(2026, 6, 15),
        )

    def _run_task(self):
        from tickets.tasks import scan_event_competition_task
        return scan_event_competition_task.apply(args=[str(self.event.id)]).result

    # --- persistence -----------------------------------------------------------

    def test_persists_ready_result_to_event(self):
        res = _result(status='ready', score=40, label='Medium')
        with patch(self.CALC, return_value=res):
            status = self._run_task()
        self.assertEqual(status, 'ready')
        self.event.refresh_from_db()
        self.assertEqual(self.event.competition_score, 40)
        self.assertEqual(self.event.competition_label, 'Medium')
        self.assertEqual(self.event.competition_status, 'ready')
        self.assertEqual(self.event.competition_data, res.to_dict())
        self.assertIsNotNone(self.event.competition_generated_at)

    def test_hash_written_only_on_success(self):
        # D5 (CRITICAL): a successful scan fingerprints the event...
        from tickets.services.market_competition import compute_input_hash
        with patch(self.CALC, return_value=_result(status='ready')):
            self._run_task()
        self.event.refresh_from_db()
        self.assertEqual(
            self.event.competition_input_hash, compute_input_hash(self.event))
        self.assertNotEqual(self.event.competition_input_hash, '')

    def test_unavailable_leaves_hash_empty_and_rescans(self):
        # D5: an unavailable scan persists status but NOT the hash, so it re-runs.
        from tickets.services.market_competition import is_competition_fresh
        from tickets.tasks import request_competition_scan
        with patch(self.CALC, return_value=_result(status='unavailable', score=0,
                                                   label='Low', summary='')):
            self._run_task()
        self.event.refresh_from_db()
        self.assertEqual(self.event.competition_status, 'unavailable')
        self.assertEqual(self.event.competition_input_hash, '')
        self.assertFalse(is_competition_fresh(self.event))
        # ...and the next trigger enqueues again rather than skipping as fresh.
        with patch('tickets.tasks.scan_event_competition_task.delay') as delay:
            self.assertEqual(
                request_competition_scan(self.org, self.event), 'scanning')
        delay.assert_called_once_with(
            str(self.event.id), genre_hints=None,
            include_domains=None, exclude_domains=None)

    def test_inconclusive_writes_hash(self):
        # D1=A: inconclusive is a completed scan and caches within the TTL.
        from tickets.services.market_competition import compute_input_hash
        with patch(self.CALC, return_value=_result(status='inconclusive', score=0,
                                                   label='Low')):
            status = self._run_task()
        self.assertEqual(status, 'inconclusive')
        self.event.refresh_from_db()
        self.assertEqual(
            self.event.competition_input_hash, compute_input_hash(self.event))

    # --- freshness / TTL (DO4) -------------------------------------------------

    def _mark_fresh(self, *, age_days=0):
        from django.utils import timezone
        from tickets.services.market_competition import compute_input_hash
        self.event.competition_input_hash = compute_input_hash(self.event)
        self.event.competition_generated_at = (
            timezone.now() - timedelta(days=age_days))
        self.event.save(update_fields=[
            'competition_input_hash', 'competition_generated_at'])

    def test_fresh_within_ttl_skips_scan(self):
        self._mark_fresh(age_days=0)
        with patch(self.CALC) as calc:
            status = self._run_task()
        calc.assert_not_called()
        self.assertEqual(status, 'fresh')

    def test_trigger_returns_fresh_without_enqueue(self):
        from tickets.tasks import request_competition_scan
        self._mark_fresh(age_days=0)
        with patch('tickets.tasks.scan_event_competition_task.delay') as delay:
            status = request_competition_scan(self.org, self.event)
        self.assertEqual(status, 'fresh')
        delay.assert_not_called()

    def test_past_ttl_rescans_despite_matching_hash(self):
        from tickets.tasks import request_competition_scan
        self._mark_fresh(age_days=8)  # older than TTL=7, hash unchanged
        with patch('tickets.tasks.scan_event_competition_task.delay') as delay:
            status = request_competition_scan(self.org, self.event)
        self.assertEqual(status, 'scanning')
        delay.assert_called_once_with(
            str(self.event.id), genre_hints=None,
            include_domains=None, exclude_domains=None)

    def test_force_overrides_freshness(self):
        from tickets.tasks import request_competition_scan
        self._mark_fresh(age_days=0)
        with patch('tickets.tasks.scan_event_competition_task.delay') as delay:
            status = request_competition_scan(self.org, self.event, force=True)
        self.assertEqual(status, 'scanning')
        delay.assert_called_once_with(
            str(self.event.id), genre_hints=None,
            include_domains=None, exclude_domains=None)

    # --- daily cap (D3) --------------------------------------------------------

    def _seed_scans_today(self, n):
        from django.utils import timezone
        for i in range(n):
            Event.objects.create(
                organization=self.org, venue=self.venue, name=f'Prior {i}',
                start_date=date(2026, 6, 1),
                competition_generated_at=timezone.now(),
            )

    @override_settings(MARKET_COMPETITION_DAILY_SCAN_CAP=2)
    def test_daily_cap_blocks_enqueue(self):
        from tickets.tasks import request_competition_scan
        self._seed_scans_today(2)
        with patch('tickets.tasks.scan_event_competition_task.delay') as delay:
            status = request_competition_scan(self.org, self.event)
        self.assertEqual(status, 'capped')
        delay.assert_not_called()

    @override_settings(MARKET_COMPETITION_DAILY_SCAN_CAP=0)
    def test_daily_cap_zero_disables_guard(self):
        from tickets.tasks import request_competition_scan
        self._seed_scans_today(5)
        with patch('tickets.tasks.scan_event_competition_task.delay') as delay:
            status = request_competition_scan(self.org, self.event)
        self.assertEqual(status, 'scanning')
        delay.assert_called_once_with(
            str(self.event.id), genre_hints=None,
            include_domains=None, exclude_domains=None)

    # --- in-progress lock (D4) -------------------------------------------------

    def test_trigger_acquires_lock_then_enqueues(self):
        from django.core.cache import cache as django_cache
        from tickets.tasks import request_competition_scan, _competition_lock_key
        with patch('tickets.tasks.scan_event_competition_task.delay') as delay:
            status = request_competition_scan(self.org, self.event)
        self.assertEqual(status, 'scanning')
        delay.assert_called_once_with(
            str(self.event.id), genre_hints=None,
            include_domains=None, exclude_domains=None)
        self.assertIsNotNone(
            django_cache.get(_competition_lock_key(self.event.id)))

    def test_held_lock_reports_in_progress(self):
        from django.core.cache import cache as django_cache
        from tickets.tasks import request_competition_scan, _competition_lock_key
        django_cache.add(_competition_lock_key(self.event.id), 1, 300)
        with patch('tickets.tasks.scan_event_competition_task.delay') as delay:
            status = request_competition_scan(self.org, self.event)
        self.assertEqual(status, 'in_progress')
        delay.assert_not_called()

    def test_lock_released_in_finally_even_on_exception(self):
        from django.core.cache import cache as django_cache
        from tickets.tasks import scan_event_competition_task, _competition_lock_key
        django_cache.add(_competition_lock_key(self.event.id), 1, 300)
        with patch(self.CALC, side_effect=RuntimeError('boom')), \
                patch.object(scan_event_competition_task, 'retry',
                             side_effect=Exception('retried')) as retry:
            scan_event_competition_task.apply(args=[str(self.event.id)])
        retry.assert_called_once()  # unexpected error => self.retry (D4/task)
        self.assertIsNone(
            django_cache.get(_competition_lock_key(self.event.id)))

    # --- loading / retry -------------------------------------------------------

    def test_missing_event_returns_missing(self):
        import uuid
        from tickets.tasks import scan_event_competition_task
        status = scan_event_competition_task.apply(
            args=[str(uuid.uuid4())]).result
        self.assertEqual(status, 'missing')

    def test_soft_deleted_event_returns_missing(self):
        from tickets.tasks import scan_event_competition_task
        self.event.delete()  # AuditBaseModel soft delete sets deleted_at
        status = scan_event_competition_task.apply(
            args=[str(self.event.id)]).result
        self.assertEqual(status, 'missing')

    def test_unexpected_error_retries(self):
        from tickets.tasks import scan_event_competition_task
        with patch(self.CALC, side_effect=RuntimeError('db down')), \
                patch.object(scan_event_competition_task, 'retry',
                             side_effect=Exception('retried')) as retry:
            scan_event_competition_task.apply(args=[str(self.event.id)])
        retry.assert_called_once()
        _, kwargs = retry.call_args
        self.assertIsInstance(kwargs.get('exc'), RuntimeError)


# ---------------------------------------------------------------------------
# Phase 5 — views + event-detail panel
# ---------------------------------------------------------------------------

@override_settings(CACHES=LOCMEM_CACHE)
class Phase5ViewTests(TestCase):
    """The scan (POST) + poll (GET) endpoints and the event-detail context.

    The scan view's gate/enqueue logic lives in ``request_competition_scan``
    (P4-tested); here we pin the *view contract* — feature-flag + org scoping +
    status passthrough — and the poll state machine, with the trigger mocked.
    """

    SCAN_VIEW = 'tickets.views.event_competition_scan'
    TRIGGER = 'tickets.tasks.request_competition_scan'

    def setUp(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()
        self.client = Client()
        self.org = Organization.objects.create(
            name='MC View Org', slug='mc-view-org',
            market_competition_enabled=True,
        )
        self.venue = Venue.objects.create(
            organization=self.org, name='The Venue', city='Austin', state='TX',
        )
        # A description gives the event a genre source, so it isn't gated by the
        # D2=A "set a genre first" prompt — the gate itself is covered separately.
        self.event = Event.objects.create(
            organization=self.org, name='Headliner Night', venue=self.venue,
            start_date=date(2026, 6, 15), start_time=time(20, 0, 0),
            description='An evening of live music.',
        )
        self.user = User.objects.create_user(
            username='mcowner@test.com', email='mcowner@test.com',
            password='pw12345',
        )
        UserProfile.objects.create(
            user=self.user, organization=self.org,
            org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.login(username='mcowner@test.com', password='pw12345')

    def _scan_url(self, event=None):
        return reverse('tickets:event_competition_scan',
                       args=[(event or self.event).id])

    def _status_url(self, event=None):
        return reverse('tickets:event_competition_status',
                       args=[(event or self.event).id])

    def _seed_result(self, status='ready'):
        self.event.competition_status = status
        self.event.competition_score = 42
        self.event.competition_label = 'Medium'
        self.event.competition_generated_at = timezone.now()
        self.event.competition_data = {
            'score': 42, 'label': 'Medium', 'status': status,
            'competitors': [{
                'name': 'Rival Show', 'date': '2026-06-16',
                'venue_name': 'Other Hall', 'metro_normalized': 'austin',
                'genre': 'rock', 'platform': 'eventbrite',
                'source_url': 'https://eventbrite.com/e/rival',
            }],
            'undated': [{
                'name': 'Mystery Gig', 'date': None, 'venue_name': 'TBD',
                'metro_normalized': 'austin', 'genre': 'rock',
                'platform': 'dice', 'source_url': 'https://dice.fm/mystery',
            }],
            'counts': {'total': 2, 'same_metro': 2, 'in_window': 1,
                       'genre_matched': 1, 'undated': 1},
            'coverage': {'queries_total': 6, 'queries_with_results': 5,
                         'ratio': 5 / 6},
            'summary': 'One comparable rock show the next night.',
            'genres': ['techno', 'house'],
        }
        self.event.save()

    # --- scan (POST) ---------------------------------------------------------

    def test_scan_requires_feature_flag(self):
        self.org.market_competition_enabled = False
        self.org.save()
        resp = self.client.post(self._scan_url())
        self.assertEqual(resp.status_code, 404)

    def test_scan_cross_org_is_404(self):
        other_org = Organization.objects.create(name='Other', slug='other-org')
        other_venue = Venue.objects.create(
            organization=other_org, name='Elsewhere', city='Dallas')
        other_event = Event.objects.create(
            organization=other_org, name='Not Yours', venue=other_venue,
            start_date=date(2026, 7, 1), start_time=time(20, 0, 0))
        resp = self.client.post(self._scan_url(other_event))
        self.assertEqual(resp.status_code, 404)

    def test_scan_returns_trigger_status_and_enqueues_once(self):
        with patch(self.TRIGGER, return_value='scanning') as trigger:
            resp = self.client.post(self._scan_url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'status': 'scanning'})
        trigger.assert_called_once()
        _, kwargs = trigger.call_args
        self.assertFalse(kwargs.get('force'))

    def test_scan_capped_passthrough(self):
        with patch(self.TRIGGER, return_value='capped'):
            resp = self.client.post(self._scan_url())
        self.assertEqual(resp.json(), {'status': 'capped'})

    def test_scan_force_flag_threaded(self):
        with patch(self.TRIGGER, return_value='scanning') as trigger:
            self.client.post(self._scan_url(), {'force': '1'})
        _, kwargs = trigger.call_args
        self.assertTrue(kwargs.get('force'))

    def test_scan_genre_override_threaded_and_forces(self):
        with patch(self.TRIGGER, return_value='scanning') as trigger:
            self.client.post(self._scan_url(), {'genres': 'Techno, House'})
        _, kwargs = trigger.call_args
        self.assertEqual(kwargs.get('genre_hints'), ['techno', 'house'])
        # A genre override is a deliberate re-scan => bypass the freshness gate.
        self.assertTrue(kwargs.get('force'))

    def test_scan_without_genres_auto_derives(self):
        with patch(self.TRIGGER, return_value='scanning') as trigger:
            self.client.post(self._scan_url())
        _, kwargs = trigger.call_args
        self.assertIsNone(kwargs.get('genre_hints'))
        self.assertFalse(kwargs.get('force'))

    def test_scan_blank_genres_matches_empty_derivation(self):
        # The test event derives no genres, so a blank field == the auto-detected
        # baseline => treated as a plain auto scan (no override, cache respected).
        with patch(self.TRIGGER, return_value='scanning') as trigger:
            self.client.post(self._scan_url(), {'genres': '  ,  '})
        _, kwargs = trigger.call_args
        self.assertIsNone(kwargs.get('genre_hints'))
        self.assertFalse(kwargs.get('force'))

    def test_scan_unchanged_auto_genres_not_an_override(self):
        # Event whose name auto-detects a genre; submitting that same genre means
        # the organizer agreed => behaves like an auto scan (no force).
        techno = Event.objects.create(
            organization=self.org, name='Techno Night', venue=self.venue,
            start_date=date(2026, 8, 1), start_time=time(21, 0, 0),
            description='Doors at 9.')
        with patch(self.TRIGGER, return_value='scanning') as trigger:
            self.client.post(self._scan_url(techno), {'genres': 'techno'})
        _, kwargs = trigger.call_args
        self.assertIsNone(kwargs.get('genre_hints'))
        self.assertFalse(kwargs.get('force'))

    def test_scan_edited_genres_are_an_override(self):
        techno = Event.objects.create(
            organization=self.org, name='Techno Night', venue=self.venue,
            start_date=date(2026, 8, 1), start_time=time(21, 0, 0),
            description='Doors at 9.')
        with patch(self.TRIGGER, return_value='scanning') as trigger:
            self.client.post(self._scan_url(techno), {'genres': 'house'})
        _, kwargs = trigger.call_args
        self.assertEqual(kwargs.get('genre_hints'), ['house'])
        self.assertTrue(kwargs.get('force'))

    def test_scan_cleared_genres_force_city_wide(self):
        # Clearing a pre-filled field differs from the derived baseline => an
        # explicit empty override that forces the city-wide density fallback.
        techno = Event.objects.create(
            organization=self.org, name='Techno Night', venue=self.venue,
            start_date=date(2026, 8, 1), start_time=time(21, 0, 0),
            description='Doors at 9.')
        with patch(self.TRIGGER, return_value='scanning') as trigger:
            self.client.post(self._scan_url(techno), {'genres': ''})
        _, kwargs = trigger.call_args
        self.assertEqual(kwargs.get('genre_hints'), [])
        self.assertTrue(kwargs.get('force'))

    def test_genre_parser_dedupes_caps_and_lowercases(self):
        from tickets.views import _parse_competition_genres, _COMPETITION_MAX_GENRES
        self.assertEqual(_parse_competition_genres(''), [])
        self.assertEqual(_parse_competition_genres(None), [])
        self.assertEqual(
            _parse_competition_genres('Rock, rock, POP'), ['rock', 'pop'])
        many = ','.join('g%d' % i for i in range(20))
        self.assertEqual(
            len(_parse_competition_genres(many)), _COMPETITION_MAX_GENRES)

    def test_panel_renders_genre_chips(self):
        resp = self.client.get(reverse('tickets:event_detail',
                                       args=[self.event.id]))
        self.assertContains(resp, 'id="competition-genre-chips"')
        # No free-text genre input — predefined options only.
        self.assertNotContains(resp, 'id="competition-genres"')

    def test_panel_renders_all_genre_options(self):
        from tickets.services.market_competition import genre_options
        resp = self.client.get(reverse('tickets:event_detail',
                                       args=[self.event.id]))
        # Every canonical genre is offered as a selectable chip (one checkbox each).
        self.assertEqual(resp.context['competition_genre_options'], genre_options())
        self.assertContains(
            resp, 'name="competition-genre"', count=len(genre_options()))
        self.assertContains(resp, '>techno</label>')

    def test_panel_preselects_auto_detected_genres(self):
        techno = Event.objects.create(
            organization=self.org, name='Techno Night', venue=self.venue,
            start_date=date(2026, 8, 1), start_time=time(21, 0, 0),
            description='Doors at 9.')
        resp = self.client.get(reverse('tickets:event_detail', args=[techno.id]))
        self.assertEqual(resp.context['competition_genre_selected'], ['techno'])

    def test_panel_no_detection_leaves_options_unselected(self):
        # Event with a description but no genre word => nothing pre-selected, but
        # the organizer can still pick from the full option list.
        plain = Event.objects.create(
            organization=self.org, name='Familiar Faces', venue=self.venue,
            start_date=date(2026, 8, 5), start_time=time(21, 0, 0),
            description='On sale now. Follow us for the lineup.')
        resp = self.client.get(reverse('tickets:event_detail', args=[plain.id]))
        self.assertEqual(resp.context['competition_genre_selected'], [])
        self.assertTrue(resp.context['competition_genre_options'])  # still offered

    def test_panel_preselects_last_scanned_genres(self):
        self._seed_result('ready')
        resp = self.client.get(reverse('tickets:event_detail',
                                       args=[self.event.id]))
        # _seed_result stored genres=['techno','house']; both chips pre-selected.
        self.assertEqual(
            resp.context['competition_genre_selected'], ['techno', 'house'])

    def test_panel_ignores_non_option_genres(self):
        # A custom Event.genre value outside the predefined list is not offered as
        # a chip and is not pre-selected (only predefined options are selectable).
        bare = Event.objects.create(
            organization=self.org, name='Weird Fest', venue=self.venue,
            start_date=date(2026, 8, 6), start_time=time(21, 0, 0),
            description='x', genre='techno, polka')  # polka isn't a canonical option
        resp = self.client.get(reverse('tickets:event_detail', args=[bare.id]))
        self.assertEqual(resp.context['competition_genre_selected'], ['techno'])
        self.assertNotIn('competition_genre_custom', resp.context)
        self.assertNotContains(resp, '>polka</label>')

    def test_scan_drops_non_option_genres(self):
        # Even a direct POST with an off-list genre keeps only predefined options.
        with patch(self.TRIGGER, return_value='scanning') as trigger:
            self.client.post(self._scan_url(), {'genres': 'techno, polka'})
        _, kwargs = trigger.call_args
        self.assertEqual(kwargs.get('genre_hints'), ['techno'])

    # --- domain scoping ------------------------------------------------------

    def test_domain_parser_extracts_hosts(self):
        from tickets.views import _parse_competition_domains, _COMPETITION_MAX_DOMAINS
        self.assertEqual(_parse_competition_domains(''), [])
        self.assertEqual(
            _parse_competition_domains('https://Eventbrite.com/e/x, dice.fm , dice.fm'),
            ['eventbrite.com', 'dice.fm'])
        many = ','.join('d%d.com' % i for i in range(40))
        self.assertEqual(
            len(_parse_competition_domains(many)), _COMPETITION_MAX_DOMAINS)

    @override_settings(MARKET_COMPETITION_INCLUDE_DOMAINS='',
                       MARKET_COMPETITION_EXCLUDE_DOMAINS='')
    def test_scan_threads_domains_and_forces(self):
        with patch(self.TRIGGER, return_value='scanning') as trigger:
            self.client.post(self._scan_url(), {
                'include_domains': 'eventbrite.com, dice.fm',
                'exclude_domains': 'ticketmaster.com',
            })
        _, kwargs = trigger.call_args
        self.assertEqual(kwargs.get('include_domains'), ['eventbrite.com', 'dice.fm'])
        self.assertEqual(kwargs.get('exclude_domains'), ['ticketmaster.com'])
        # Domains differ from the (empty) defaults => an explicit re-scan.
        self.assertTrue(kwargs.get('force'))

    @override_settings(MARKET_COMPETITION_INCLUDE_DOMAINS='eventbrite.com',
                       MARKET_COMPETITION_EXCLUDE_DOMAINS='')
    def test_scan_keeping_default_domains_not_forced(self):
        with patch(self.TRIGGER, return_value='scanning') as trigger:
            self.client.post(self._scan_url(), {
                'include_domains': 'eventbrite.com',  # == the default
                'exclude_domains': '',
            })
        _, kwargs = trigger.call_args
        self.assertEqual(kwargs.get('include_domains'), ['eventbrite.com'])
        self.assertEqual(kwargs.get('exclude_domains'), [])
        self.assertFalse(kwargs.get('force'))

    @override_settings(MARKET_COMPETITION_INCLUDE_DOMAINS='eventbrite.com,dice.fm',
                       MARKET_COMPETITION_EXCLUDE_DOMAINS='ticketmaster.com',
                       MARKET_COMPETITION_SOURCES='web')
    def test_panel_prefills_domain_defaults(self):
        resp = self.client.get(reverse('tickets:event_detail',
                                       args=[self.event.id]))
        self.assertEqual(resp.context['competition_include_domains'],
                         'eventbrite.com, dice.fm')
        self.assertEqual(resp.context['competition_exclude_domains'],
                         'ticketmaster.com')
        self.assertContains(resp, 'id="competition-include-domains"')
        self.assertContains(resp, 'id="competition-exclude-domains"')

    def test_domain_controls_hidden_when_posh_only(self):
        with override_settings(MARKET_COMPETITION_SOURCES='posh'):
            resp = self.client.get(reverse('tickets:event_detail',
                                           args=[self.event.id]))
        self.assertFalse(resp.context['competition_domain_controls'])
        self.assertNotContains(resp, 'id="competition-include-domains"')

    def test_status_payload_includes_genres(self):
        self._seed_result('ready')
        data = self.client.get(self._status_url()).json()
        self.assertEqual(data['genres'], ['techno', 'house'])

    def test_scan_rejects_get(self):
        resp = self.client.get(self._scan_url())
        self.assertEqual(resp.status_code, 405)

    # --- poll (GET) ----------------------------------------------------------

    def test_status_idle_when_never_scanned(self):
        resp = self.client.get(self._status_url())
        self.assertEqual(resp.json(), {'status': 'idle'})

    def test_status_scanning_when_lock_held(self):
        from django.core.cache import cache as django_cache
        from tickets.tasks import _competition_lock_key
        django_cache.add(_competition_lock_key(self.event.id), 1, 300)
        resp = self.client.get(self._status_url())
        self.assertEqual(resp.json(), {'status': 'scanning'})

    def test_status_ready_returns_payload(self):
        self._seed_result('ready')
        data = self.client.get(self._status_url()).json()
        self.assertEqual(data['status'], 'ready')
        self.assertEqual(data['counts']['genre_matched'], 1)
        self.assertEqual(len(data['competitors']), 1)
        self.assertEqual(len(data['undated']), 1)
        self.assertEqual(data['coverage']['queries_with_results'], 5)
        self.assertEqual(data['summary'],
                         'One comparable rock show the next night.')
        self.assertIsNotNone(data['generated_at'])

    @override_settings(MARKET_COMPETITION_SHOW_SCORE=False)
    def test_status_payload_hides_score_by_default(self):
        self._seed_result('ready')
        data = self.client.get(self._status_url()).json()
        self.assertFalse(data['show_score'])
        # The numeric value still rides in the payload; the panel gates display.
        self.assertEqual(data['score'], 42)

    @override_settings(MARKET_COMPETITION_SHOW_SCORE=True)
    def test_status_payload_shows_score_when_gate_open(self):
        self._seed_result('ready')
        data = self.client.get(self._status_url()).json()
        self.assertTrue(data['show_score'])

    def test_status_unavailable_distinct_from_ready_zero(self):
        self._seed_result('unavailable')
        data = self.client.get(self._status_url()).json()
        self.assertEqual(data['status'], 'unavailable')

    def test_status_inconclusive_distinct_from_ready_zero(self):
        self._seed_result('inconclusive')
        data = self.client.get(self._status_url()).json()
        self.assertEqual(data['status'], 'inconclusive')

    def test_status_requires_feature_flag(self):
        self.org.market_competition_enabled = False
        self.org.save()
        resp = self.client.get(self._status_url())
        self.assertEqual(resp.status_code, 404)

    # --- event_detail context + panel ---------------------------------------

    def test_event_detail_carries_cached_payload(self):
        self._seed_result('ready')
        resp = self.client.get(reverse('tickets:event_detail',
                                       args=[self.event.id]))
        self.assertEqual(resp.status_code, 200)
        payload = resp.context['competition_payload']
        self.assertIsNotNone(payload)
        self.assertEqual(payload['status'], 'ready')
        self.assertFalse(resp.context['competition_show_score'])

    def test_event_detail_payload_none_when_never_scanned(self):
        resp = self.client.get(reverse('tickets:event_detail',
                                       args=[self.event.id]))
        self.assertIsNone(resp.context['competition_payload'])

    def test_panel_hidden_when_flag_off(self):
        self.org.market_competition_enabled = False
        self.org.save()
        resp = self.client.get(reverse('tickets:event_detail',
                                       args=[self.event.id]))
        self.assertNotContains(resp, 'id="competition-card"')

    def test_panel_shown_when_flag_on(self):
        resp = self.client.get(reverse('tickets:event_detail',
                                       args=[self.event.id]))
        self.assertContains(resp, 'id="competition-card"')

    # --- role gating ---------------------------------------------------------

    def test_attendee_redirected_from_scan(self):
        attendee = User.objects.create_user(
            username='att@test.com', email='att@test.com', password='pw12345')
        UserProfile.objects.create(
            user=attendee, organization=self.org,
            role=UserProfile.Role.ATTENDEE)
        c = Client()
        c.login(username='att@test.com', password='pw12345')
        resp = c.post(self._scan_url())
        self.assertEqual(resp.status_code, 302)

    # --- genre-source gate (D2=A) --------------------------------------------

    def _bare_external_event(self):
        # External, no description, no ticket_link, no genre => no genre source.
        from tickets.models import TICKETING_TYPE_EXTERNAL
        return Event.objects.create(
            organization=self.org, name='Mystery Show', venue=self.venue,
            start_date=date(2026, 9, 1), start_time=time(20, 0, 0),
            ticketing_type=TICKETING_TYPE_EXTERNAL)

    def test_scan_blocked_when_no_genre_source(self):
        bare = self._bare_external_event()
        with patch(self.TRIGGER, return_value='scanning') as trigger:
            resp = self.client.post(self._scan_url(bare))
        self.assertEqual(resp.json(), {'status': 'needs_genre'})
        trigger.assert_not_called()

    def test_panel_prompts_to_set_genre_when_no_source(self):
        bare = self._bare_external_event()
        resp = self.client.get(reverse('tickets:event_detail', args=[bare.id]))
        self.assertTrue(resp.context['competition_needs_genre'])
        self.assertContains(resp, 'Set a genre')
        self.assertContains(
            resp, reverse('tickets:event_edit', args=[bare.id]))
        # Scan controls are not rendered while blocked.
        self.assertNotContains(resp, 'id="competition-genres"')

    def test_set_genre_unblocks_the_tool(self):
        bare = self._bare_external_event()
        bare.genre = 'techno'
        bare.save()
        resp = self.client.get(reverse('tickets:event_detail', args=[bare.id]))
        self.assertFalse(resp.context['competition_needs_genre'])
        self.assertContains(resp, 'id="competition-genre-chips"')
        self.assertEqual(resp.context['competition_genre_selected'], ['techno'])

    def test_direct_event_never_blocked(self):
        from tickets.models import TICKETING_TYPE_DIRECT
        direct = Event.objects.create(
            organization=self.org, name='House Party', venue=self.venue,
            start_date=date(2026, 9, 2), start_time=time(20, 0, 0),
            ticketing_type=TICKETING_TYPE_DIRECT)  # Cue buy page always exists
        resp = self.client.get(reverse('tickets:event_detail', args=[direct.id]))
        self.assertFalse(resp.context['competition_needs_genre'])

    def test_external_event_with_ticket_link_not_blocked(self):
        from tickets.models import TICKETING_TYPE_EXTERNAL
        linked = Event.objects.create(
            organization=self.org, name='Linked Show', venue=self.venue,
            start_date=date(2026, 9, 3), start_time=time(20, 0, 0),
            ticketing_type=TICKETING_TYPE_EXTERNAL,
            ticket_link='https://eventbrite.com/e/linked')
        resp = self.client.get(reverse('tickets:event_detail', args=[linked.id]))
        self.assertFalse(resp.context['competition_needs_genre'])


# ---------------------------------------------------------------------------
# Phase 5 — genre sources: description/buy-page detection + gate (D1/D2/D3/D4)
# ---------------------------------------------------------------------------

@override_settings(CACHES=LOCMEM_CACHE)
class GenreSourceServiceTests(TestCase):
    """The service helpers behind the transparent genre field + the D2=A gate."""

    def setUp(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()
        self.org = Organization.objects.create(name='Gen Org', slug='gen-org')
        self.venue = Venue.objects.create(
            organization=self.org, name='Hall', city='Austin', state='TX')

    def _event(self, **kw):
        from tickets.models import TICKETING_TYPE_EXTERNAL
        defaults = dict(
            organization=self.org, name='Show', venue=self.venue,
            start_date=date(2026, 6, 1), start_time=time(20, 0, 0),
            ticketing_type=TICKETING_TYPE_EXTERNAL)
        defaults.update(kw)
        return Event.objects.create(**defaults)

    def test_parse_genres_normalizes(self):
        from tickets.services.market_competition import parse_genres
        self.assertEqual(parse_genres('  Techno , techno, HOUSE '),
                         ['techno', 'house'])
        self.assertEqual(parse_genres(''), [])
        self.assertEqual(len(parse_genres(','.join(str(i) for i in range(20)),
                                          max_genres=3)), 3)

    def test_genre_keywords_match_on_word_boundaries(self):
        from tickets.services.market_competition.calculator import genres_in_text
        # Mid-word collisions must NOT trigger a genre (the posh "edm"-from-"travel"
        # and "rap"-from-"photography" bug).
        self.assertEqual(genres_in_text('due to travel complications'), [])
        self.assertEqual(genres_in_text('event photography and video notice'), [])
        # Real genre words still match, including legit plurals.
        self.assertEqual(genres_in_text('dj sets by tba'), ['dj'])
        self.assertEqual(
            genres_in_text('hip-hop r&b dancehall afrobeats'),
            ['hip-hop', 'r&b', 'afrobeats', 'dancehall'])  # dancehall is its own genre

    def test_derive_uses_extra_text(self):
        from tickets.services.market_competition import derive_genre_hints
        ev = self._event(name='Untitled')  # name alone => nothing
        self.assertEqual(derive_genre_hints(ev), [])
        self.assertEqual(
            derive_genre_hints(ev, extra_text='A night of deep house music'),
            ['house'])

    def test_resolve_prefers_explicit_genre(self):
        from tickets.services.market_competition import resolve_event_genres
        ev = self._event(name='Techno Night', genre='jazz, soul')
        # Explicit Event.genre wins over what the name would auto-detect.
        self.assertEqual(resolve_event_genres(ev), ['jazz', 'soul'])

    def test_resolve_falls_back_to_detection(self):
        from tickets.services.market_competition import resolve_event_genres
        ev = self._event(name='Techno Night')
        self.assertEqual(resolve_event_genres(ev), ['techno'])
        self.assertEqual(
            resolve_event_genres(ev, extra_text='country bluegrass'),
            ['techno', 'country'])

    def test_has_genre_source_matrix(self):
        from tickets.models import TICKETING_TYPE_DIRECT
        from tickets.services.market_competition import event_has_genre_source
        self.assertFalse(event_has_genre_source(self._event()))  # bare external
        self.assertTrue(event_has_genre_source(self._event(description='x')))
        self.assertTrue(event_has_genre_source(self._event(genre='techno')))
        self.assertTrue(event_has_genre_source(
            self._event(ticket_link='https://eventbrite.com/e/x')))
        self.assertTrue(event_has_genre_source(
            self._event(ticketing_type=TICKETING_TYPE_DIRECT)))

    def test_buy_page_url_external_only(self):
        from tickets.models import TICKETING_TYPE_DIRECT
        from tickets.services.market_competition import event_buy_page_url
        self.assertEqual(
            event_buy_page_url(self._event(ticket_link='https://dice.fm/e/x')),
            'https://dice.fm/e/x')
        # We never fetch our own Cue-hosted direct page.
        self.assertEqual(
            event_buy_page_url(self._event(ticketing_type=TICKETING_TYPE_DIRECT)),
            '')

    def test_input_hash_includes_genre(self):
        from tickets.services.market_competition import compute_input_hash
        ev = self._event()
        before = compute_input_hash(ev)
        ev.genre = 'techno'
        self.assertNotEqual(compute_input_hash(ev), before)


@override_settings(CACHES=LOCMEM_CACHE, DEBUG=False)
class BuyPageFetchTests(TestCase):
    """The SSRF-guarded, cached, fail-silent buy-page probe (D1=A)."""

    FETCH = 'tickets.services.market_competition.page_fetch.requests.get'

    def setUp(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()

    def _resp(self, *, status=200, ctype='text/html', body=b'<html>x</html>'):
        r = MagicMock()
        r.raise_for_status.return_value = None
        r.headers = {'Content-Type': ctype}
        r.content = body
        r.encoding = 'utf-8'
        if status >= 400:
            r.raise_for_status.side_effect = requests.HTTPError('boom')
        return r

    def test_success_strips_tags(self):
        from tickets.services.market_competition.page_fetch import fetch_page_text
        html = b'<html><head><title>Techno Rave</title></head><body>' \
               b'<script>var x=1</script><p>Deep house all night</p></body></html>'
        with patch(self.FETCH, return_value=self._resp(body=html)):
            text = fetch_page_text('https://eventbrite.com/e/x')
        self.assertIn('Techno Rave', text)
        self.assertIn('Deep house all night', text)
        self.assertNotIn('var x=1', text)  # script stripped

    def test_blocks_ssrf_url(self):
        from tickets.services.market_competition.page_fetch import fetch_page_text
        with patch(self.FETCH) as get:
            self.assertEqual(fetch_page_text('http://169.254.169.254/latest'), '')
            get.assert_not_called()  # blocked before any request

    def test_non_html_returns_empty(self):
        from tickets.services.market_competition.page_fetch import fetch_page_text
        with patch(self.FETCH, return_value=self._resp(ctype='application/pdf')):
            self.assertEqual(fetch_page_text('https://eventbrite.com/x.pdf'), '')

    def test_timeout_returns_empty(self):
        from tickets.services.market_competition.page_fetch import fetch_page_text
        with patch(self.FETCH, side_effect=requests.Timeout('slow')):
            self.assertEqual(fetch_page_text('https://eventbrite.com/e/x'), '')

    def test_caches_result(self):
        from tickets.services.market_competition.page_fetch import fetch_page_text
        html = b'<html><body>jazz quartet</body></html>'
        with patch(self.FETCH, return_value=self._resp(body=html)) as get:
            fetch_page_text('https://eventbrite.com/e/x')
            fetch_page_text('https://eventbrite.com/e/x')
        self.assertEqual(get.call_count, 1)  # second call served from cache


@override_settings(CACHES=LOCMEM_CACHE, TAVILY_API_KEY='k',
                   MARKET_COMPETITION_SOURCES='web')
class CalculatorGenreWiringTests(TestCase):
    """calculate() resolves Event.genre / the fetched buy page into the scan."""

    def setUp(self):
        self.org = Organization.objects.create(name='Calc Org', slug='calc-org')
        self.venue = Venue.objects.create(
            organization=self.org, name='Hall', city='Austin', state='TX')

    def _event(self, **kw):
        from tickets.models import TICKETING_TYPE_EXTERNAL
        defaults = dict(
            organization=self.org, name='Untitled Show', venue=self.venue,
            start_date=date(2026, 6, 1), start_time=time(20, 0, 0),
            ticketing_type=TICKETING_TYPE_EXTERNAL)
        defaults.update(kw)
        return Event.objects.create(**defaults)

    def _run_capture(self, event):
        """Call calculate() with the scan stubbed; return the genre_hints used."""
        from tickets.services.market_competition import calculate_event_competition
        stub = ScanResult(competitors=[], target_metro_normalized='',
                          coverage={'queries_total': 0, 'queries_with_results': 0,
                                    'ratio': 1.0}, ok=False)
        with patch('tickets.services.market_competition.scanner.scan_competitors',
                   return_value=stub) as scan:
            calculate_event_competition(self.org, event)
        return scan.call_args[0][2]  # 3rd positional arg: genre_hints

    def test_explicit_genre_used(self):
        ev = self._event(genre='jazz', ticket_link='https://eventbrite.com/e/x')
        with patch('tickets.services.market_competition.page_fetch.fetch_page_text') as f:
            hints = self._run_capture(ev)
        self.assertEqual(hints, ['jazz'])
        f.assert_not_called()  # explicit genre => no need to fetch the page

    def test_buy_page_feeds_detection(self):
        ev = self._event(ticket_link='https://eventbrite.com/e/x')
        with patch('tickets.services.market_competition.page_fetch.fetch_page_text',
                   return_value='Underground techno warehouse rave') as f:
            hints = self._run_capture(ev)
        f.assert_called_once_with('https://eventbrite.com/e/x')
        self.assertIn('techno', hints)


# ---------------------------------------------------------------------------
# posh.vip source + multi-source fan-out (D1=B)
# ---------------------------------------------------------------------------

def _posh_item(title, start, venue="The Mayan", city="Los Angeles",
               url="https://posh.vip/e/x", desc=""):
    return {
        "eventTitle": title,
        "startDateTime": start,          # ISO string or None
        "venueName": venue,
        "city": city,
        "eventUrl": url,
        "description": desc,
    }


@override_settings(CACHES=LOCMEM_CACHE, APIFY_TOKEN="tok",
                   MARKET_COMPETITION_SOURCES="posh")
class PoshVipSourceTests(TestCase):
    """The posh.vip Apify source — mapping, genre, dedupe, coverage (LLM-free)."""

    FETCH = "tickets.services.market_competition.sources.fetch_posh_events"

    def setUp(self):
        self.org = Organization.objects.create(name="Posh Org", slug="posh-org")
        self.venue = Venue.objects.create(
            organization=self.org, name="The Echo", city="Los Angeles", state="CA")
        self.event = Event.objects.create(
            organization=self.org, name="Bloom", venue=self.venue,
            start_date=date(2026, 10, 25), end_date=date(2026, 10, 25),
            start_time=time(21, 0, 0))

    def _fetch(self, items):
        from tickets.services.market_competition.sources import PoshVipSource
        with patch(self.FETCH, return_value=items) as f:
            res = PoshVipSource().fetch(self.org, self.event, ["r&b"])
        return res, f

    def test_maps_fields_and_infers_genre(self):
        items = [
            _posh_item("Deep House Rooftop", "2026-10-26T22:00:00",
                       venue="Exchange", url="https://posh.vip/e/dh"),
            _posh_item("CLUB OVO - DRAKE NIGHT", "2026-10-23T22:00:00"),
        ]
        res, _ = self._fetch(items)
        self.assertTrue(res.ok)
        self.assertEqual(res.target_metro_normalized, "los angeles")
        by_name = {c.name: c for c in res.competitors}
        self.assertEqual(by_name["Deep House Rooftop"].genre, "house")
        self.assertEqual(by_name["Deep House Rooftop"].platform, "posh")
        self.assertEqual(by_name["Deep House Rooftop"].date, date(2026, 10, 26))
        self.assertEqual(by_name["Deep House Rooftop"].metro_normalized, "los angeles")
        # Branded title with no genre word => empty genre (keyword-only, by design).
        self.assertEqual(by_name["CLUB OVO - DRAKE NIGHT"].genre, "")

    def test_when_maps_from_target_date(self):
        from django.utils import timezone
        soon = timezone.localdate() + timedelta(days=3)
        far = timezone.localdate() + timedelta(days=40)
        for d, expected in ((soon, "This Week"), (far, "This Month")):
            self.event.start_date = d
            self.event.end_date = d
            self.event.save()
            with patch(self.FETCH, return_value=[]) as f:
                from tickets.services.market_competition.sources import PoshVipSource
                PoshVipSource().fetch(self.org, self.event, [])
            _, kwargs = f.call_args
            self.assertEqual(f.call_args[0][1], expected)

    def test_self_exclusion_and_dedupe(self):
        items = [
            _posh_item("Bloom", "2026-10-25T21:00:00", venue="The Echo"),   # the target
            _posh_item("Rival", "2026-10-25T21:00:00", venue="Club A"),
            _posh_item("Rival", "2026-10-25T21:00:00", venue="Club A"),      # dupe
        ]
        res, _ = self._fetch(items)
        names = [c.name for c in res.competitors]
        self.assertEqual(names, ["Rival"])  # target excluded, dupe collapsed

    def test_undated_event_kept_with_none_date(self):
        res, _ = self._fetch([_posh_item("Mystery", None)])
        self.assertEqual(res.competitors[0].date, None)

    def test_utc_datetime_converted_to_local_date(self):
        from tickets.services.market_competition.sources import _posh_local_date
        # 04:00 UTC on Oct 24 is 21:00 Pacific on Oct 23 (the real event night).
        self.assertEqual(
            _posh_local_date('2026-10-24T04:00:00.000Z', 'America/Los_Angeles'),
            date(2026, 10, 23))
        # End-to-end: the mapped competitor date is the local date, not the UTC one.
        item = _posh_item('Familiar Faces: LA', '2026-10-24T04:00:00.000Z')
        item['timezone'] = 'America/Los_Angeles'
        res, _ = self._fetch([item])
        self.assertEqual(res.competitors[0].date, date(2026, 10, 23))

    def test_unavailable_when_fetch_fails(self):
        from tickets.services.market_competition.sources import PoshVipSource
        with patch(self.FETCH, return_value=None):  # no token / provider error
            res = PoshVipSource().fetch(self.org, self.event, [])
        self.assertFalse(res.ok)

    def test_unavailable_without_city(self):
        from tickets.services.market_competition.sources import PoshVipSource
        self.venue.city = ""
        self.venue.save()
        with patch(self.FETCH) as f:
            res = PoshVipSource().fetch(self.org, self.event, [])
        f.assert_not_called()
        self.assertFalse(res.ok)

    def test_coverage_zero_when_feed_misses_target_date(self):
        # Feed only has events well before the target => not a trustworthy "clear".
        res, _ = self._fetch([_posh_item("Old Thing", "2026-09-01T21:00:00")])
        self.assertEqual(res.coverage["queries_with_results"], 0)

    def test_coverage_hit_when_feed_reaches_target(self):
        res, _ = self._fetch([_posh_item("On Target", "2026-10-25T21:00:00")])
        self.assertEqual(res.coverage["queries_with_results"], 1)

    def test_calculate_end_to_end_with_posh(self):
        # Full calculator path on posh only — no TAVILY/OpenAI extraction needed.
        from tickets.services.market_competition import calculate_event_competition
        items = [_posh_item("R&B Night", "2026-10-26T21:00:00", desc="soul and r&b")]
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(
            content="n", usage_metadata={"input_tokens": 1, "output_tokens": 1,
                                         "total_tokens": 2})
        with patch(self.FETCH, return_value=items), \
                patch("langchain_openai.ChatOpenAI", return_value=llm):
            result = calculate_event_competition(self.org, self.event)
        self.assertEqual(result.status, "ready")
        self.assertEqual([c.name for c in result.competitors], ["R&B Night"])


@override_settings(CACHES=LOCMEM_CACHE)
class SourceRegistryTests(TestCase):
    def test_default_is_posh(self):
        from tickets.services.market_competition.sources import (
            get_active_sources, PoshVipSource)
        with override_settings(MARKET_COMPETITION_SOURCES="posh"):
            srcs = get_active_sources()
        self.assertEqual([type(s) for s in srcs], [PoshVipSource])

    def test_web_and_unknown(self):
        from tickets.services.market_competition.sources import (
            get_active_sources, WebSearchSource, PoshVipSource)
        with override_settings(MARKET_COMPETITION_SOURCES="web,bogus"):
            srcs = get_active_sources()
        self.assertEqual([type(s) for s in srcs], [WebSearchSource])
        with override_settings(MARKET_COMPETITION_SOURCES=""):
            srcs = get_active_sources()  # empty => posh fallback
        self.assertEqual([type(s) for s in srcs], [PoshVipSource])

    def test_fan_out_merges_and_dedupes(self):
        from tickets.services.market_competition import sources as S
        org = Organization.objects.create(name="FO", slug="fo-org")
        venue = Venue.objects.create(organization=org, name="V", city="LA")
        event = Event.objects.create(
            organization=org, name="T", venue=venue,
            start_date=date(2026, 10, 25), start_time=time(20, 0, 0))

        shared = CompetitorEvent(name="Same", date=date(2026, 10, 25),
                                 venue_name="X", metro_normalized="la",
                                 genre="", platform="posh", source_url="u")
        posh_res = ScanResult(competitors=[shared],
                              target_metro_normalized="la",
                              coverage={"queries_total": 1, "queries_with_results": 1,
                                        "ratio": 1.0}, ok=True)
        web_res = ScanResult(
            competitors=[shared, CompetitorEvent(
                name="WebOnly", date=date(2026, 10, 25), venue_name="Y",
                metro_normalized="la", genre="", platform="web", source_url="u2")],
            target_metro_normalized="la",
            coverage={"queries_total": 4, "queries_with_results": 2, "ratio": 0.5},
            ok=True)
        with override_settings(MARKET_COMPETITION_SOURCES="posh,web"), \
                patch.object(S.PoshVipSource, "fetch", return_value=posh_res), \
                patch.object(S.WebSearchSource, "fetch", return_value=web_res):
            merged = S.fetch_competitors(org, event, [])
        names = sorted(c.name for c in merged.competitors)
        self.assertEqual(names, ["Same", "WebOnly"])  # shared collapsed across sources
        self.assertTrue(merged.ok)
        self.assertEqual(merged.coverage["queries_total"], 5)


@override_settings(CACHES=LOCMEM_CACHE, APIFY_TOKEN="tok",
                   MARKET_COMPETITION_POSH_MAX_EVENTS=300)
class ApifyClientTests(TestCase):
    """The posh.vip Apify HTTP wrapper."""

    POST = "tickets.services.market_competition.apify_client.requests.post"

    def setUp(self):
        from django.core.cache import cache as django_cache
        django_cache.clear()

    def test_sends_maxitems_query_param(self):
        # Pay-per-result run-sync requires maxItems (caps results + reserved charge).
        from tickets.services.market_competition.apify_client import fetch_posh_events
        resp = MagicMock(status_code=200)
        resp.raise_for_status.return_value = None
        resp.json.return_value = [{"eventTitle": "X"}]
        with patch(self.POST, return_value=resp) as post:
            out = fetch_posh_events("Los Angeles", "This Month")
        self.assertEqual(out, [{"eventTitle": "X"}])
        _, kwargs = post.call_args
        self.assertEqual(kwargs["params"]["maxItems"], 300)

    def test_none_without_token(self):
        from tickets.services.market_competition.apify_client import fetch_posh_events
        with override_settings(APIFY_TOKEN=""):
            self.assertIsNone(fetch_posh_events("LA", "This Month"))

    def _ok_resp(self, items=None):
        resp = MagicMock(status_code=200)
        resp.raise_for_status.return_value = None
        resp.json.return_value = items if items is not None else [{"eventTitle": "X"}]
        return resp

    def test_feed_is_cached_across_calls(self):
        # Cost efficiency: a second scan of the same (city, window) reuses the paid
        # fetch instead of re-charging Apify.
        from tickets.services.market_competition.apify_client import fetch_posh_events
        with patch(self.POST, return_value=self._ok_resp()) as post:
            fetch_posh_events("Los Angeles", "This Month")
            fetch_posh_events("Los Angeles", "This Month")
        self.assertEqual(post.call_count, 1)  # second served from cache

    def test_db_cache_survives_cache_clear(self):
        # The whole point of the durable cache: a dev autoreload / Redis eviction
        # (simulated by clearing the django cache) must NOT cause a re-charge.
        from django.core.cache import cache as django_cache
        from tickets.models import MarketCompetitionFeedCache
        from tickets.services.market_competition.apify_client import fetch_posh_events
        with patch(self.POST, return_value=self._ok_resp()) as post:
            fetch_posh_events("Los Angeles", "This Month")
            django_cache.clear()  # wipe the ephemeral cache (reload/eviction)
            out = fetch_posh_events("Los Angeles", "This Month")
        self.assertEqual(post.call_count, 1)  # DB served the second call
        self.assertEqual(out, [{"eventTitle": "X"}])
        self.assertEqual(MarketCompetitionFeedCache.objects.count(), 1)

    def test_stale_db_entry_refetches(self):
        from tickets.services.market_competition.apify_client import fetch_posh_events
        with patch(self.POST, return_value=self._ok_resp()) as post:
            with override_settings(MARKET_COMPETITION_POSH_CACHE_TTL=3600):
                fetch_posh_events("Los Angeles", "This Month")
            with override_settings(MARKET_COMPETITION_POSH_CACHE_TTL=0):
                fetch_posh_events("Los Angeles", "This Month")  # TTL=0 => stale
        self.assertEqual(post.call_count, 2)

    def test_http_error_returns_none_and_logs_body(self):
        from tickets.services.market_competition.apify_client import fetch_posh_events
        resp = MagicMock(status_code=400)
        resp.text = '{"error":{"type":"max-items-must-be-greater-than-zero"}}'
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
        with patch(self.POST, return_value=resp), \
                self.assertLogs("tickets.services.market_competition.apify_client",
                                level="WARNING") as logs:
            out = fetch_posh_events("LA", "This Month")
        self.assertIsNone(out)
        self.assertTrue(any("max-items-must-be-greater-than-zero" in m for m in logs.output))
