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
from django.core.management import call_command
from django.test import TestCase, override_settings

from .models import AITokenUsage, Event, EventTalent, Organization, Venue
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
                   MARKET_COMPETITION_PLATFORMS='eventbrite,dice,seetickets')
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
                   MARKET_COMPETITION_PLATFORMS='eventbrite,dice,seetickets')
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
        delay.assert_called_once_with(str(self.event.id))

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
        delay.assert_called_once_with(str(self.event.id))

    def test_force_overrides_freshness(self):
        from tickets.tasks import request_competition_scan
        self._mark_fresh(age_days=0)
        with patch('tickets.tasks.scan_event_competition_task.delay') as delay:
            status = request_competition_scan(self.org, self.event, force=True)
        self.assertEqual(status, 'scanning')
        delay.assert_called_once_with(str(self.event.id))

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
        delay.assert_called_once_with(str(self.event.id))

    # --- in-progress lock (D4) -------------------------------------------------

    def test_trigger_acquires_lock_then_enqueues(self):
        from django.core.cache import cache as django_cache
        from tickets.tasks import request_competition_scan, _competition_lock_key
        with patch('tickets.tasks.scan_event_competition_task.delay') as delay:
            status = request_competition_scan(self.org, self.event)
        self.assertEqual(status, 'scanning')
        delay.assert_called_once_with(str(self.event.id))
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
