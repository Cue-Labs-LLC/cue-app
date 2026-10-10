"""Tests for the Market Competition Agent.

Phase 0 — data-model foundations: the new ``Event.competition_*`` fields, the
``Organization.market_competition_enabled`` flag, and the
``AITokenUsage.FEATURE_MARKET_COMPETITION`` choice.

Phase 1 — the pure, offline core: ``score_competition`` (the deterministic
scoring math) and ``build_queries`` (the fixed search-query plan).
"""

from datetime import date, time

from django.test import TestCase, override_settings

from .models import AITokenUsage, Event, Organization, Venue
from .services.market_competition import (
    CompetitionResult,
    CompetitorEvent,
    TargetEvent,
    build_queries,
    score_competition,
)
from .services.market_competition import scoring


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
