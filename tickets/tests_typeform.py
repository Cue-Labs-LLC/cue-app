"""Tests for the Typeform integration."""
import base64
import hashlib
import hmac
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from tickets.models import (
    AITokenUsage, Event, ExternalSurveyResponse, ExternalSurveyUpload, Organization,
    OrganizationMembership, TypeformFormSubscription, UserProfile, Venue,
)
from tickets.services.typeform import field_mapping
from tickets.services.typeform.event_matcher import (
    EventCandidate, MatchResult, SurveyEventMatcher, apply_top_candidate,
)
from tickets.services.typeform.ingest import ingest_response


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_org_and_user(slug='typeform-org', org_role='owner'):
    org = Organization.objects.create(name='Typeform Org', slug=slug)
    user = User.objects.create_user(username=f'{slug}-user', password='pw')
    UserProfile.objects.create(user=user, organization=org, org_role=org_role)
    OrganizationMembership.objects.create(organization=org, user=user, org_role=org_role)
    return org, user


def _make_subscription(org, **kwargs):
    upload = ExternalSurveyUpload.objects.create(
        organization=org,
        filename='Typeform: Test form',
        status=ExternalSurveyUpload.Status.COMPLETED,
    )
    defaults = {
        'organization': org,
        'form_id': 'abc123',
        'form_title': 'Test form',
        'webhook_secret': 'secret-xyz',
        'upload': upload,
        'field_map': {
            'rating_ref': 'overall_rating',
            'nps_ref': 'nps_score',
            'email_ref': 'email',
            'city_ref': 'city',
            'enjoyed_ref': 'enjoyed',
            'feedback_ref': 'text_feedback',
        },
    }
    defaults.update(kwargs)
    return TypeformFormSubscription.objects.create(**defaults)


def _sample_webhook_payload(response_id='resp_123', submitted_at=None):
    if submitted_at is None:
        submitted_at = '2026-05-15T20:30:00Z'
    return {
        'event_type': 'form_response',
        'form_response': {
            'form_id': 'abc123',
            'token': response_id,
            'response_id': response_id,
            'submitted_at': submitted_at,
            'answers': [
                {
                    'type': 'number',
                    'number': 5,
                    'field': {'ref': 'rating_ref', 'type': 'rating'},
                },
                {
                    'type': 'number',
                    'number': 9,
                    'field': {'ref': 'nps_ref', 'type': 'opinion_scale'},
                },
                {
                    'type': 'email',
                    'email': 'attendee@example.com',
                    'field': {'ref': 'email_ref', 'type': 'email'},
                },
                {
                    'type': 'text',
                    'text': 'San Francisco',
                    'field': {'ref': 'city_ref', 'type': 'short_text'},
                },
                {
                    'type': 'choices',
                    'choices': {'labels': ['DJ set', 'Lighting']},
                    'field': {'ref': 'enjoyed_ref', 'type': 'multiple_choice'},
                },
                {
                    'type': 'text',
                    'text': 'Loved the headliner at Cobra Lounge!',
                    'field': {'ref': 'feedback_ref', 'type': 'long_text'},
                },
            ],
        },
    }


# ── Field mapping ──────────────────────────────────────────────────────────

class TypeformFieldMappingTests(TestCase):
    def test_auto_field_map_detects_common_question_types(self):
        form = {
            'fields': [
                {'ref': 'rating', 'type': 'rating', 'title': 'How was it?',
                 'properties': {}},
                {'ref': 'nps', 'type': 'opinion_scale', 'title': 'Would you recommend?',
                 'properties': {'steps': 11}},
                {'ref': 'email', 'type': 'email', 'title': 'Your email',
                 'properties': {}},
                {'ref': 'city', 'type': 'short_text', 'title': 'Which city?',
                 'properties': {}},
                {'ref': 'enjoyed', 'type': 'multiple_choice', 'title': 'What did you enjoy?',
                 'properties': {'allow_multiple_selection': True}},
                {'ref': 'genres', 'type': 'multiple_choice', 'title': 'Favorite music genre?',
                 'properties': {'allow_multiple_selection': True}},
                {'ref': 'improvements', 'type': 'multiple_choice', 'title': 'What could we improve?',
                 'properties': {'allow_multiple_selection': True}},
                {'ref': 'feedback', 'type': 'long_text', 'title': 'Any other comments?',
                 'properties': {}},
                {'ref': 'raffle', 'type': 'email', 'title': 'Raffle email to win prizes',
                 'properties': {}},
            ],
        }
        mapping = field_mapping.auto_field_map(form)
        self.assertEqual(mapping['rating'], 'overall_rating')
        self.assertEqual(mapping['nps'], 'nps_score')
        self.assertEqual(mapping['email'], 'email')
        self.assertEqual(mapping['city'], 'city')
        self.assertEqual(mapping['enjoyed'], 'enjoyed')
        self.assertEqual(mapping['genres'], 'genres')
        self.assertEqual(mapping['improvements'], 'improvements')
        self.assertEqual(mapping['feedback'], 'text_feedback')
        self.assertEqual(mapping['raffle'], 'raffle_email')

    def test_auto_field_map_skips_unknown_or_unmappable_fields(self):
        form = {
            'fields': [
                {'ref': 'random', 'type': 'short_text', 'title': 'Pet name?',
                 'properties': {}},
                {'ref': 'no_id', 'type': 'long_text', 'title': 'Tell us anything'},
            ],
        }
        mapping = field_mapping.auto_field_map(form)
        # 'random' short_text doesn't have a "city"-style title — skipped.
        self.assertNotIn('random', mapping)
        # long_text → text_feedback (and 'no_id' uses its id or ref).
        self.assertEqual(mapping.get('no_id'), 'text_feedback')


# ── Ingest ─────────────────────────────────────────────────────────────────

class TypeformIngestTests(TestCase):
    def setUp(self):
        self.org, _ = _make_org_and_user(slug='ingest-org')
        self.subscription = _make_subscription(self.org)

    def test_ingest_response_creates_external_survey_response(self):
        payload = _sample_webhook_payload()['form_response']
        response, created = ingest_response(self.subscription, payload)

        self.assertTrue(created)
        self.assertIsNotNone(response)
        self.assertEqual(response.organization, self.org)
        self.assertEqual(response.typeform_response_id, 'resp_123')
        self.assertEqual(response.email, 'attendee@example.com')
        self.assertEqual(response.city, 'San Francisco')
        self.assertEqual(response.overall_rating, '5')
        self.assertEqual(response.nps_score, 9)
        self.assertEqual(response.enjoyed, ['DJ set', 'Lighting'])
        self.assertEqual(response.upload, self.subscription.upload)
        self.assertIn('Cobra Lounge', response.text_feedback)

    def test_ingest_response_is_idempotent_on_response_id(self):
        payload = _sample_webhook_payload()['form_response']
        response_a, created_a = ingest_response(self.subscription, payload)
        response_b, created_b = ingest_response(self.subscription, payload)

        self.assertTrue(created_a)
        self.assertFalse(created_b)
        self.assertEqual(response_a.id, response_b.id)
        self.assertEqual(
            ExternalSurveyResponse.objects.filter(typeform_response_id='resp_123').count(),
            1,
        )

    def test_ingest_response_skips_payload_without_response_id(self):
        payload = _sample_webhook_payload()['form_response']
        payload.pop('response_id')
        payload.pop('token')
        response, created = ingest_response(self.subscription, payload)
        self.assertIsNone(response)
        self.assertFalse(created)


# ── Webhook view ──────────────────────────────────────────────────────────

class TypeformWebhookViewTests(TestCase):
    def setUp(self):
        self.org, _ = _make_org_and_user(slug='hook-org')
        self.subscription = _make_subscription(self.org)
        self.client = Client()

    def _sign(self, body: bytes) -> str:
        digest = hmac.new(
            self.subscription.webhook_secret.encode('utf-8'),
            body, hashlib.sha256,
        ).digest()
        return f'sha256={base64.b64encode(digest).decode("ascii")}'

    @patch('tickets.tasks.match_survey_response_to_event_task.delay')
    def test_valid_signature_creates_response_and_queues_match(self, mock_delay):
        body = json.dumps(_sample_webhook_payload()).encode('utf-8')
        signature = self._sign(body)

        url = reverse('tickets:typeform_webhook', args=[str(self.subscription.id)])
        response = self.client.post(
            url, data=body, content_type='application/json',
            HTTP_TYPEFORM_SIGNATURE=signature,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ExternalSurveyResponse.objects.count(), 1)
        mock_delay.assert_called_once()

    def test_invalid_signature_rejects_request(self):
        body = json.dumps(_sample_webhook_payload()).encode('utf-8')
        url = reverse('tickets:typeform_webhook', args=[str(self.subscription.id)])
        response = self.client.post(
            url, data=body, content_type='application/json',
            HTTP_TYPEFORM_SIGNATURE='sha256=AAAAdeadbeef',
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(ExternalSurveyResponse.objects.count(), 0)

    def test_inactive_subscription_returns_404(self):
        self.subscription.is_active = False
        self.subscription.save(update_fields=['is_active'])
        body = json.dumps(_sample_webhook_payload()).encode('utf-8')
        signature = self._sign(body)
        url = reverse('tickets:typeform_webhook', args=[str(self.subscription.id)])
        response = self.client.post(
            url, data=body, content_type='application/json',
            HTTP_TYPEFORM_SIGNATURE=signature,
        )
        self.assertEqual(response.status_code, 404)


# ── Event matcher ─────────────────────────────────────────────────────────

class SurveyEventMatcherTests(TestCase):
    def setUp(self):
        self.org, _ = _make_org_and_user(slug='match-org')
        self.subscription = _make_subscription(self.org)
        self.venue = Venue.objects.create(
            organization=self.org, name='Cobra Lounge', city='San Francisco', state='CA',
        )
        self.event = Event.objects.create(
            organization=self.org, venue=self.venue,
            name='Pulse SF May', summary='House music night',
            start_date=date(2026, 5, 14), end_date=date(2026, 5, 14),
            start_time=time(20, 0), end_time=time(23, 0),
        )
        payload = _sample_webhook_payload(submitted_at='2026-05-15T20:30:00Z')['form_response']
        self.response, _ = ingest_response(self.subscription, payload)

    @patch('langchain_openai.ChatOpenAI')
    def test_suggest_records_token_usage_and_returns_match(self, mock_chat):
        fake_llm = MagicMock()
        structured = MagicMock()
        fake_match = MatchResult(candidates=[
            EventCandidate(event_id=str(self.event.id), confidence=0.92,
                           reasoning='Date + venue mention')
        ])
        structured.invoke.return_value = {
            'parsed': fake_match,
            'raw': MagicMock(usage_metadata={
                'input_tokens': 50, 'output_tokens': 20, 'total_tokens': 70,
            }),
            'parsing_error': None,
        }
        fake_llm.with_structured_output.return_value = structured
        mock_chat.return_value = fake_llm

        result = SurveyEventMatcher(self.org).suggest(self.response)
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].event_id, str(self.event.id))
        # Token usage was recorded under the new feature.
        self.assertTrue(
            AITokenUsage.objects.filter(
                organization=self.org,
                feature=AITokenUsage.FEATURE_TYPEFORM_EVENT_MATCH,
            ).exists()
        )

    def test_apply_top_candidate_writes_suggested_event_when_confident(self):
        match = MatchResult(candidates=[
            EventCandidate(event_id=str(self.event.id), confidence=0.85,
                           reasoning='Date proximity')
        ])
        saved = apply_top_candidate(self.response, match)
        self.assertTrue(saved)
        self.response.refresh_from_db()
        self.assertEqual(self.response.suggested_event_id, self.event.id)
        self.assertEqual(self.response.match_confidence, Decimal('0.850'))

    def test_apply_top_candidate_leaves_suggestion_blank_when_below_threshold(self):
        match = MatchResult(candidates=[
            EventCandidate(event_id=str(self.event.id), confidence=0.10,
                           reasoning='Weak signal'),
        ])
        saved = apply_top_candidate(self.response, match)
        self.assertTrue(saved)
        self.response.refresh_from_db()
        self.assertIsNone(self.response.suggested_event)
        self.assertEqual(self.response.match_confidence, Decimal('0.100'))
        self.assertIn('Weak signal', self.response.match_reasoning)


# ── Connect / disconnect views ─────────────────────────────────────────────

class TypeformConnectViewTests(TestCase):
    def setUp(self):
        self.org, self.user = _make_org_and_user(slug='connect-org', org_role='owner')
        self.client = Client()
        self.client.force_login(self.user)

    @patch('tickets.services.typeform.client.TypeformClient.validate_token')
    def test_connect_saves_token_when_validation_succeeds(self, mock_validate):
        mock_validate.return_value = {'email': 'org@example.com', 'alias': 'org'}
        response = self.client.post(reverse('tickets:typeform_connect'), {
            'access_token': 'tfp_live_abc123',
        })
        self.assertEqual(response.status_code, 302)
        self.org.refresh_from_db()
        self.assertEqual(self.org.typeform_access_token, 'tfp_live_abc123')
        self.assertEqual(self.org.typeform_account_email, 'org@example.com')
        self.assertIsNotNone(self.org.typeform_validated_at)

    @patch('tickets.services.typeform.client.TypeformClient.validate_token')
    def test_connect_does_not_save_invalid_token(self, mock_validate):
        from tickets.services.typeform.client import TypeformAPIError
        mock_validate.side_effect = TypeformAPIError('Unauthorized')
        response = self.client.post(reverse('tickets:typeform_connect'), {
            'access_token': 'bad-token',
        })
        self.assertEqual(response.status_code, 302)
        self.org.refresh_from_db()
        self.assertEqual(self.org.typeform_access_token, '')
