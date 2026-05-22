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
