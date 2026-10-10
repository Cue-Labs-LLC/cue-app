"""Tests for the Market Competition Agent.

Phase 0 — data-model foundations only: the new ``Event.competition_*`` fields,
the ``Organization.market_competition_enabled`` flag, and the
``AITokenUsage.FEATURE_MARKET_COMPETITION`` choice. No behavior yet.
"""

from datetime import date, time

from django.test import TestCase

from .models import AITokenUsage, Event, Organization, Venue


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
