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
            'definition': {
                'fields': [
                    {'id': 'fid_rating', 'ref': 'rating_ref', 'type': 'rating',
                     'title': 'How was it?'},
                    {'id': 'fid_nps', 'ref': 'nps_ref', 'type': 'opinion_scale',
                     'title': 'Would you recommend?'},
                    {'id': 'fid_email', 'ref': 'email_ref', 'type': 'email',
                     'title': 'Your email'},
                    {'id': 'fid_city', 'ref': 'city_ref', 'type': 'short_text',
                     'title': 'Which city?'},
                    {'id': 'fid_enjoyed', 'ref': 'enjoyed_ref', 'type': 'multiple_choice',
                     'title': 'What did you enjoy?'},
                    {'id': 'fid_feedback', 'ref': 'feedback_ref', 'type': 'long_text',
                     'title': 'Any other comments?'},
                ],
            },
            'answers': [
                {
                    'type': 'number', 'number': 5,
                    'field': {'id': 'fid_rating', 'ref': 'rating_ref', 'type': 'rating'},
                },
                {
                    'type': 'number', 'number': 9,
                    'field': {'id': 'fid_nps', 'ref': 'nps_ref', 'type': 'opinion_scale'},
                },
                {
                    'type': 'email', 'email': 'attendee@example.com',
                    'field': {'id': 'fid_email', 'ref': 'email_ref', 'type': 'email'},
                },
                {
                    'type': 'text', 'text': 'San Francisco',
                    'field': {'id': 'fid_city', 'ref': 'city_ref', 'type': 'short_text'},
                },
                {
                    'type': 'choices', 'choices': {'labels': ['DJ set', 'Lighting']},
                    'field': {'id': 'fid_enjoyed', 'ref': 'enjoyed_ref', 'type': 'multiple_choice'},
                },
                {
                    'type': 'text', 'text': 'Loved the headliner at Cobra Lounge!',
                    'field': {'id': 'fid_feedback', 'ref': 'feedback_ref', 'type': 'long_text'},
                },
            ],
        },
    }


# ── Ingest ─────────────────────────────────────────────────────────────────

class TypeformIngestTests(TestCase):
    def setUp(self):
        self.org, _ = _make_org_and_user(slug='ingest-org')
        self.subscription = _make_subscription(self.org)

    def test_ingest_response_stores_raw_answers_verbatim(self):
        payload = _sample_webhook_payload()['form_response']
        response, created = ingest_response(self.subscription, payload)

        self.assertTrue(created)
        self.assertIsNotNone(response)
        self.assertEqual(response.organization, self.org)
        self.assertEqual(response.typeform_response_id, 'resp_123')
        self.assertEqual(response.upload, self.subscription.upload)

        # Raw answers populated for every Typeform answer, with titles from the definition.
        self.assertEqual(len(response.raw_answers), 6)
        by_title = {a['title']: a for a in response.raw_answers}
        self.assertEqual(by_title['How was it?']['value'], 5)
        self.assertEqual(by_title['Would you recommend?']['value'], 9)
        self.assertEqual(by_title['Your email']['value'], 'attendee@example.com')
        self.assertEqual(by_title['Which city?']['value'], 'San Francisco')
        self.assertEqual(by_title['What did you enjoy?']['value'], ['DJ set', 'Lighting'])
        self.assertIn('Cobra Lounge', by_title['Any other comments?']['value'])

        # Structured CSV-shaped columns stay blank for Typeform-sourced rows.
        self.assertEqual(response.email, '')
        self.assertEqual(response.city, '')
        self.assertEqual(response.overall_rating, '')
        self.assertIsNone(response.nps_score)
        self.assertEqual(response.enjoyed, [])
        self.assertEqual(response.text_feedback, '')

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

    def test_ingest_response_populates_structured_columns_when_field_map_set(self):
        # Configure a mapping that targets every interesting structured column.
        self.subscription.field_map = {
            'rating_ref': 'overall_rating',
            'nps_ref': 'nps_score',
            'email_ref': 'email',
            'city_ref': 'city',
            'enjoyed_ref': 'enjoyed',
            'feedback_ref': 'text_feedback',
        }
        self.subscription.save(update_fields=['field_map'])

        payload = _sample_webhook_payload()['form_response']
        response, created = ingest_response(self.subscription, payload)

        self.assertTrue(created)
        # Raw answers still populated unchanged.
        self.assertEqual(len(response.raw_answers), 6)
        # Mapped structured columns now have values.
        self.assertEqual(response.overall_rating, '5')
        self.assertEqual(response.nps_score, 9)
        self.assertEqual(response.email, 'attendee@example.com')
        self.assertEqual(response.city, 'San Francisco')
        self.assertEqual(response.enjoyed, ['DJ set', 'Lighting'])
        self.assertIn('Cobra Lounge', response.text_feedback)


# ── Field mapping service ─────────────────────────────────────────────────

class TypeformFieldMappingTests(TestCase):
    def test_auto_field_map_detects_common_question_types(self):
        from tickets.services.typeform.field_mapping import auto_field_map

        form = {
            'fields': [
                {'ref': 'rating', 'type': 'rating', 'title': 'How was it?', 'properties': {}},
                {'ref': 'nps', 'type': 'opinion_scale', 'title': 'Recommend?',
                 'properties': {'steps': 11}},
                {'ref': 'email', 'type': 'email', 'title': 'Your email', 'properties': {}},
                {'ref': 'city', 'type': 'short_text', 'title': 'Which city?', 'properties': {}},
                {'ref': 'enjoyed', 'type': 'multiple_choice', 'title': 'What did you enjoy?',
                 'properties': {'allow_multiple_selection': True}},
                {'ref': 'genres', 'type': 'multiple_choice', 'title': 'Music genre?',
                 'properties': {'allow_multiple_selection': True}},
                {'ref': 'feedback', 'type': 'long_text', 'title': 'Any other comments?',
                 'properties': {}},
                {'ref': 'raffle', 'type': 'email', 'title': 'Raffle email to win',
                 'properties': {}},
            ],
        }
        mapping = auto_field_map(form)
        self.assertEqual(mapping['rating'], 'overall_rating')
        self.assertEqual(mapping['nps'], 'nps_score')
        self.assertEqual(mapping['email'], 'email')
        self.assertEqual(mapping['city'], 'city')
        self.assertEqual(mapping['enjoyed'], 'enjoyed')
        self.assertEqual(mapping['genres'], 'genres')
        self.assertEqual(mapping['feedback'], 'text_feedback')
        self.assertEqual(mapping['raffle'], 'raffle_email')

    def test_apply_field_map_projects_values_with_correct_types(self):
        from tickets.services.typeform.field_mapping import apply_field_map

        raw_answers = [
            {'id': 'fr', 'ref': 'rating', 'type': 'rating', 'title': 'Rating', 'value': 5},
            {'id': 'fn', 'ref': 'nps', 'type': 'opinion_scale', 'title': 'NPS', 'value': 9},
            {'id': 'fc', 'ref': 'city', 'type': 'short_text', 'title': 'City', 'value': 'SF'},
            {'id': 'fe', 'ref': 'enjoyed', 'type': 'choices', 'title': 'Enjoyed',
             'value': ['DJ', 'Lights']},
            {'id': 'ft', 'ref': 'fb', 'type': 'long_text', 'title': 'Feedback', 'value': 'Loved it'},
        ]
        field_map = {
            'rating': 'overall_rating',
            'nps': 'nps_score',
            'city': 'city',
            'enjoyed': 'enjoyed',
            'fb': 'text_feedback',
        }
        result = apply_field_map(raw_answers, field_map)
        self.assertEqual(result['overall_rating'], '5')      # coerced to str
        self.assertEqual(result['nps_score'], 9)             # int 0-10
        self.assertEqual(result['city'], 'SF')
        self.assertEqual(result['enjoyed'], ['DJ', 'Lights'])  # list preserved
        self.assertEqual(result['text_feedback'], 'Loved it')

    def test_apply_field_map_handles_empty_inputs(self):
        from tickets.services.typeform.field_mapping import apply_field_map

        self.assertEqual(apply_field_map([], {'x': 'email'}), {})
        self.assertEqual(apply_field_map([{'ref': 'x', 'value': 'a'}], {}), {})

    def test_apply_field_map_truncates_to_model_max_lengths(self):
        from tickets.services.typeform.field_mapping import apply_field_map

        raw = [{'ref': 'c', 'value': 'x' * 250}]
        result = apply_field_map(raw, {'c': 'city'})
        self.assertEqual(len(result['city']), 100)  # city max_length=100

    def test_apply_field_map_drops_invalid_nps_values(self):
        from tickets.services.typeform.field_mapping import apply_field_map

        # Out-of-range and non-numeric NPS values get coerced to None.
        for bad in [11, -1, 'high', None]:
            result = apply_field_map(
                [{'ref': 'n', 'value': bad}], {'n': 'nps_score'},
            )
            self.assertIsNone(result.get('nps_score'))

    def test_auto_field_map_recurses_into_group_fields(self):
        from tickets.services.typeform.field_mapping import auto_field_map

        form = {'fields': [
            {'ref': 'sect1', 'type': 'group', 'title': 'Section 1', 'properties': {'fields': [
                {'ref': 'rating', 'type': 'rating', 'title': 'How was it?', 'properties': {}},
                {'ref': 'email', 'type': 'email', 'title': 'Your email', 'properties': {}},
            ]}},
            {'ref': 'sect2', 'type': 'inline_group', 'title': 'Section 2', 'properties': {'fields': [
                {'ref': 'feedback', 'type': 'long_text', 'title': 'Comments?', 'properties': {}},
            ]}},
        ]}
        mapping = auto_field_map(form)
        # Group wrappers contribute no entries; their children do.
        self.assertNotIn('sect1', mapping)
        self.assertNotIn('sect2', mapping)
        self.assertEqual(mapping['rating'], 'overall_rating')
        self.assertEqual(mapping['email'], 'email')
        self.assertEqual(mapping['feedback'], 'text_feedback')

    def test_flatten_form_fields_skips_non_answerable_types(self):
        from tickets.services.typeform.field_mapping import flatten_form_fields

        form = {'fields': [
            {'ref': 'welcome', 'type': 'statement', 'title': 'Welcome'},
            {'ref': 'thankyou', 'type': 'thankyou_screen', 'title': 'Thanks'},
            {'ref': 'r', 'type': 'rating', 'title': 'How was it?', 'properties': {}},
        ]}
        flat = flatten_form_fields(form)
        refs = [f['ref'] for f in flat]
        self.assertEqual(refs, ['r'])
        self.assertEqual(flat[0]['group_title'], '')

    def test_flatten_form_fields_propagates_parent_group_title(self):
        from tickets.services.typeform.field_mapping import flatten_form_fields

        form = {'fields': [
            {'ref': 'group', 'type': 'group', 'title': 'Demographics', 'properties': {'fields': [
                {'ref': 'city', 'type': 'short_text', 'title': 'City', 'properties': {}},
            ]}},
        ]}
        flat = flatten_form_fields(form)
        self.assertEqual(flat[0]['group_title'], 'Demographics')


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
        # Prompt body should include the raw answers (not the legacy structured columns).
        prompt_arg = structured.invoke.call_args.args[0][1]['content']
        self.assertIn('Cobra Lounge', prompt_arg)
        self.assertIn('Which city?', prompt_arg)

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


# ── Surveys tab on the event detail page ──────────────────────────────────

class EventSurveyTabViewTests(TestCase):
    """Per-form card structure (event_detail GET) + unlink view."""

    def setUp(self):
        self.org, self.user = _make_org_and_user(slug='evt-tab-org', org_role='owner')
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
        self.linked = ingest_response(
            self.subscription,
            _sample_webhook_payload(response_id='r_linked')['form_response'],
        )[0]
        self.linked.event = self.event
        self.linked.save()

        self.client = Client()
        self.client.force_login(self.user)

    def test_event_detail_attaches_linked_responses_to_each_subscription(self):
        response = self.client.get(reverse('tickets:event_detail', args=[self.event.id]))
        self.assertEqual(response.status_code, 200)
        subs = response.context['typeform_subscriptions']
        self.assertEqual(len(subs), 1)
        # Each subscription carries its own linked-response list as an attribute.
        linked_ids = {r.id for r in subs[0].linked_responses}
        self.assertIn(self.linked.id, linked_ids)

    def test_event_detail_excludes_inactive_subscriptions(self):
        inactive = _make_subscription(self.org, form_id='zzz-inactive', is_active=False)
        response = self.client.get(reverse('tickets:event_detail', args=[self.event.id]))
        sub_ids = {s.id for s in response.context['typeform_subscriptions']}
        self.assertIn(self.subscription.id, sub_ids)
        self.assertNotIn(inactive.id, sub_ids)

    def test_unlink_view_clears_event_link(self):
        url = reverse('tickets:event_survey_unlink', args=[self.event.id])
        response = self.client.post(url, {'response_id': str(self.linked.id)})
        self.assertEqual(response.status_code, 302)
        self.linked.refresh_from_db()
        self.assertIsNone(self.linked.event_id)

    def test_event_detail_renders_inline_suggest_section_with_match_url(self):
        response = self.client.get(reverse('tickets:event_detail', args=[self.event.id]))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        # The inline auto-load entrypoint must be present so the JS can fire on tab show.
        self.assertIn('survey-suggest-section', body)
        expected = (
            reverse('tickets:event_survey_match', args=[self.event.id])
            + f'?sub_id={self.subscription.id}&format=json'
        )
        # data-match-url should point at the per-form match endpoint.
        self.assertIn(expected, body)

    def test_unlink_view_refuses_other_orgs_response(self):
        other_org, _ = _make_org_and_user(slug='other-unlink-org')
        other_sub = _make_subscription(other_org, form_id='zzz-other')
        other_resp = ingest_response(
            other_sub, _sample_webhook_payload(response_id='r_other')['form_response'],
        )[0]
        other_resp.event = self.event  # spoof attempt
        other_resp.save()

        url = reverse('tickets:event_survey_unlink', args=[self.event.id])
        self.client.post(url, {'response_id': str(other_resp.id)})
        other_resp.refresh_from_db()
        # event FK should still be set — other org's caller can't clear it.
        self.assertEqual(other_resp.event_id, self.event.id)


# ── EventSurveyMatcher (event → responses ranker) ─────────────────────────

class EventSurveyMatcherRankTests(TestCase):
    def setUp(self):
        self.org, _ = _make_org_and_user(slug='rank-org')
        self.subscription = _make_subscription(self.org)
        self.venue = Venue.objects.create(
            organization=self.org, name='Cobra Lounge', city='San Francisco', state='CA',
        )
        self.event = Event.objects.create(
            organization=self.org, venue=self.venue,
            name='Pulse SF May',
            start_date=date(2026, 5, 14), end_date=date(2026, 5, 14),
            start_time=time(20, 0), end_time=time(23, 0),
        )
        self.r1 = ingest_response(
            self.subscription, _sample_webhook_payload(response_id='r1')['form_response'],
        )[0]
        self.r2 = ingest_response(
            self.subscription, _sample_webhook_payload(response_id='r2')['form_response'],
        )[0]

    @patch('langchain_openai.ChatOpenAI')
    def test_rank_returns_candidates_sorted_by_confidence_and_records_token_usage(self, mock_chat):
        from tickets.services.typeform.event_matcher import (
            EventSurveyMatcher, ResponseCandidate, ResponseRankResult,
        )

        fake_llm = MagicMock()
        structured = MagicMock()
        structured.invoke.return_value = {
            'parsed': ResponseRankResult(candidates=[
                ResponseCandidate(response_id=str(self.r2.id), confidence=0.4, reasoning='Some signal'),
                ResponseCandidate(response_id=str(self.r1.id), confidence=0.9, reasoning='Strong match'),
            ]),
            'raw': MagicMock(usage_metadata={
                'input_tokens': 100, 'output_tokens': 30, 'total_tokens': 130,
            }),
            'parsing_error': None,
        }
        fake_llm.with_structured_output.return_value = structured
        mock_chat.return_value = fake_llm

        result = EventSurveyMatcher(self.org).rank(self.event, [self.r1, self.r2])
        ids = [c.response_id for c in result.candidates]
        self.assertEqual(ids, [str(self.r1.id), str(self.r2.id)])  # sorted desc by confidence
        self.assertTrue(
            AITokenUsage.objects.filter(
                organization=self.org, feature=AITokenUsage.FEATURE_TYPEFORM_EVENT_MATCH,
            ).exists()
        )


# ── Match view (returns ranked JSON) ──────────────────────────────────────

class EventSurveyMatchViewTests(TestCase):
    def setUp(self):
        self.org, self.user = _make_org_and_user(slug='match-view-org', org_role='owner')
        self.subscription = _make_subscription(self.org)
        self.venue = Venue.objects.create(organization=self.org, name='V', city='X', state='Y')
        self.event = Event.objects.create(
            organization=self.org, venue=self.venue, name='E',
            start_date=date(2026, 5, 14), end_date=date(2026, 5, 14),
            start_time=time(20, 0), end_time=time(23, 0),
        )
        # One unlinked candidate (in range), one already linked (excluded), one out-of-range.
        self.cand = ingest_response(
            self.subscription, _sample_webhook_payload(response_id='r_cand')['form_response'],
        )[0]
        self.already_linked = ingest_response(
            self.subscription, _sample_webhook_payload(response_id='r_done')['form_response'],
        )[0]
        self.already_linked.event = self.event
        self.already_linked.save()
        self.client = Client()
        self.client.force_login(self.user)

    @patch('tickets.services.typeform.event_matcher.EventSurveyMatcher.rank')
    @patch('tickets.services.typeform.client.TypeformClient.list_responses')
    def test_match_json_excludes_already_linked_and_returns_ranked_candidates(
        self, mock_list, mock_rank,
    ):
        from tickets.services.typeform.event_matcher import (
            ResponseCandidate, ResponseRankResult,
        )
        # Token is unset → list_responses won't be called, but mock for safety:
        mock_list.return_value = {'items': [], 'page_count': 0, 'page': 1}
        mock_rank.return_value = ResponseRankResult(candidates=[
            ResponseCandidate(response_id=str(self.cand.id), confidence=0.81, reasoning='X'),
        ])

        url = reverse('tickets:event_survey_match', args=[self.event.id]) + '?format=json'
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']['candidates']
        ids = [c['response_id'] for c in data]
        self.assertIn(str(self.cand.id), ids)
        self.assertNotIn(str(self.already_linked.id), ids)
        self.assertEqual(data[0]['confidence_pct'], 81)
        self.assertEqual(data[0]['confidence_class'], 'bg-success')


# ── Apply view (links selected responses to event) ────────────────────────

class EventSurveyApplyViewTests(TestCase):
    def setUp(self):
        self.org, self.user = _make_org_and_user(slug='apply-view-org', org_role='owner')
        self.subscription = _make_subscription(self.org)
        self.venue = Venue.objects.create(organization=self.org, name='V', city='X', state='Y')
        self.event = Event.objects.create(
            organization=self.org, venue=self.venue, name='E',
            start_date=date(2026, 5, 14), end_date=date(2026, 5, 14),
            start_time=time(20, 0), end_time=time(23, 0),
        )
        self.r1 = ingest_response(
            self.subscription, _sample_webhook_payload(response_id='ra1')['form_response'],
        )[0]
        self.r2 = ingest_response(
            self.subscription, _sample_webhook_payload(response_id='ra2')['form_response'],
        )[0]
        self.client = Client()
        self.client.force_login(self.user)

    def test_apply_links_selected_responses_with_confidence_and_reasoning(self):
        url = reverse('tickets:event_survey_apply', args=[self.event.id])
        response = self.client.post(url, {
            'response_id': [str(self.r1.id), str(self.r2.id)],
            'confidence': ['0.9', '0.55'],
            'reasoning': ['Strong', 'Maybe'],
        })
        self.assertEqual(response.status_code, 302)
        self.r1.refresh_from_db()
        self.r2.refresh_from_db()
        self.assertEqual(self.r1.event_id, self.event.id)
        self.assertEqual(self.r2.event_id, self.event.id)
        self.assertEqual(self.r1.match_confidence, Decimal('0.9'))
        self.assertEqual(self.r2.match_confidence, Decimal('0.55'))
        self.assertEqual(self.r1.match_reasoning, 'Strong')

    def test_apply_silently_skips_cross_org_responses(self):
        other_org, _ = _make_org_and_user(slug='cross-org-apply')
        other_sub = _make_subscription(other_org, form_id='zzz')
        other_resp = ingest_response(
            other_sub, _sample_webhook_payload(response_id='other')['form_response'],
        )[0]
        url = reverse('tickets:event_survey_apply', args=[self.event.id])
        self.client.post(url, {
            'response_id': [str(other_resp.id)],
            'confidence': ['0.9'], 'reasoning': ['x'],
        })
        other_resp.refresh_from_db()
        self.assertIsNone(other_resp.event_id)

    def test_apply_does_not_overwrite_already_linked_responses(self):
        # r1 is already linked to a different event — apply must not steal it.
        other_event = Event.objects.create(
            organization=self.org, venue=self.venue, name='Other',
            start_date=date(2026, 5, 1), end_date=date(2026, 5, 1),
            start_time=time(20, 0), end_time=time(23, 0),
        )
        self.r1.event = other_event
        self.r1.save()
        url = reverse('tickets:event_survey_apply', args=[self.event.id])
        self.client.post(url, {
            'response_id': [str(self.r1.id)],
            'confidence': ['0.9'], 'reasoning': ['x'],
        })
        self.r1.refresh_from_db()
        self.assertEqual(self.r1.event_id, other_event.id)


# ── Field-mapping editor view ─────────────────────────────────────────────

class TypeformFormMappingViewTests(TestCase):
    SAMPLE_DEFINITION = {
        'id': 'abc123',
        'title': 'Test form',
        'fields': [
            {'id': 'fid_rating', 'ref': 'rating_ref', 'type': 'rating',
             'title': 'How was it?', 'properties': {}},
            {'id': 'fid_nps', 'ref': 'nps_ref', 'type': 'opinion_scale',
             'title': 'Would you recommend?', 'properties': {'steps': 11}},
            {'id': 'fid_email', 'ref': 'email_ref', 'type': 'email',
             'title': 'Your email', 'properties': {}},
        ],
    }

    def setUp(self):
        self.org, self.user = _make_org_and_user(slug='mapview-org', org_role='owner')
        self.org.typeform_access_token = 'tfp_test_token'
        self.org.save(update_fields=['typeform_access_token'])
        self.subscription = _make_subscription(self.org)
        self.client = Client()
        self.client.force_login(self.user)

    @patch('tickets.services.typeform.client.TypeformClient.get_form')
    def test_get_prefills_dropdowns_with_auto_detected_mapping(self, mock_get_form):
        mock_get_form.return_value = self.SAMPLE_DEFINITION
        url = reverse('tickets:typeform_form_mapping', args=[self.subscription.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # Each question rendered; auto_field_map suggests overall_rating + nps_score + email.
        body = response.content.decode()
        self.assertIn('rating_ref', body)
        self.assertIn('overall_rating', body)
        self.assertIn('nps_score', body)

    @patch('tickets.services.typeform.client.TypeformClient.get_form')
    def test_post_saves_field_map_to_subscription(self, mock_get_form):
        mock_get_form.return_value = self.SAMPLE_DEFINITION
        url = reverse('tickets:typeform_form_mapping', args=[self.subscription.id])
        response = self.client.post(url, {
            'map_rating_ref': 'overall_rating',
            'map_nps_ref': 'nps_score',
            'map_email_ref': 'email',
        })
        self.assertEqual(response.status_code, 302)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.field_map, {
            'rating_ref': 'overall_rating',
            'nps_ref': 'nps_score',
            'email_ref': 'email',
        })

    @patch('tickets.services.typeform.client.TypeformClient.get_form')
    def test_post_with_invalid_target_is_ignored(self, mock_get_form):
        mock_get_form.return_value = self.SAMPLE_DEFINITION
        url = reverse('tickets:typeform_form_mapping', args=[self.subscription.id])
        self.client.post(url, {
            'map_rating_ref': 'not_a_real_field',  # not in TARGET_FIELDS → dropped
            'map_email_ref': 'email',
        })
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.field_map, {'email_ref': 'email'})

    @patch('tickets.services.typeform.client.TypeformClient.get_form')
    def test_post_with_backfill_reapplies_mapping_to_existing_rows(self, mock_get_form):
        mock_get_form.return_value = self.SAMPLE_DEFINITION
        # Pre-existing response ingested before any mapping was configured.
        existing = ingest_response(
            self.subscription,
            _sample_webhook_payload(response_id='r_old')['form_response'],
        )[0]
        self.assertEqual(existing.email, '')
        self.assertEqual(existing.overall_rating, '')

        url = reverse('tickets:typeform_form_mapping', args=[self.subscription.id])
        self.client.post(url, {
            'map_rating_ref': 'overall_rating',
            'map_nps_ref': 'nps_score',
            'map_email_ref': 'email',
            'backfill': '1',
        })

        existing.refresh_from_db()
        self.assertEqual(existing.overall_rating, '5')
        self.assertEqual(existing.nps_score, 9)
        self.assertEqual(existing.email, 'attendee@example.com')

    @patch('tickets.services.typeform.client.TypeformClient.get_form')
    def test_get_renders_sample_values_from_recent_responses(self, mock_get_form):
        mock_get_form.return_value = self.SAMPLE_DEFINITION
        # Ingest one response so its raw_answers can seed the sample column.
        ingest_response(
            self.subscription,
            _sample_webhook_payload(response_id='r_sample')['form_response'],
        )
        url = reverse('tickets:typeform_form_mapping', args=[self.subscription.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        # Sample values for refs that exist in the form definition should appear in
        # the rendered Sample answers column (email_ref and rating_ref are in
        # SAMPLE_DEFINITION; the webhook payload provides answers for both).
        self.assertIn('attendee@example.com', body)
        # The rating answer (number 5) should also render as a sample.
        self.assertIn('&ldquo;5&rdquo;', body)

    @patch('tickets.services.typeform.client.TypeformClient.get_form')
    def test_get_renders_no_responses_placeholder_when_no_ingest_yet(self, mock_get_form):
        mock_get_form.return_value = self.SAMPLE_DEFINITION
        url = reverse('tickets:typeform_form_mapping', args=[self.subscription.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn('No responses yet', response.content.decode())

    @patch('tickets.services.typeform.client.TypeformClient.get_form')
    def test_get_walks_into_group_field_and_renders_each_child(self, mock_get_form):
        # Wrap the same three questions in a group container — the editor should
        # surface one row per child, not a single opaque group row.
        mock_get_form.return_value = {
            'id': 'abc123', 'title': 'Test form',
            'fields': [{
                'id': 'fid_group', 'ref': 'group_ref', 'type': 'group',
                'title': 'Section A',
                'properties': {'fields': self.SAMPLE_DEFINITION['fields']},
            }],
        }
        url = reverse('tickets:typeform_form_mapping', args=[self.subscription.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        # Each child question's ref renders as a dropdown name; the group's does not.
        self.assertIn('name="map_rating_ref"', body)
        self.assertIn('name="map_nps_ref"', body)
        self.assertIn('name="map_email_ref"', body)
        self.assertNotIn('name="map_group_ref"', body)
        # Group title is surfaced above the question title for context.
        self.assertIn('Section A', body)
