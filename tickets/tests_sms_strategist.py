"""Tests for the AI SMS Campaign Strategist: plan generation, gating, org scoping,
token metering, and launching a step into the composer."""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase, Client, override_settings
from django.urls import reverse

from .models import (
    Organization, UserProfile, Customer, CustomerTag,
    SMSCampaign, SMSCampaignPlan, SMSMessageRecipient, AITokenUsage,
    Venue, Event,
)
from .services.sms_strategist import (
    CampaignPlan, PlanStep, generate_campaign_plan,
    _top_prior_campaigns, _recent_campaign_bodies,
)


def _fake_plan():
    return CampaignPlan(
        strategy_summary='Three touches ramping to the event.',
        steps=[
            PlanStep(purpose='announcement', audience='All subscribers', offset_days=14, send_time='18:00',
                     message='Tickets are live for the show. Grab yours: https://cue.test/t/abc/',
                     rationale='Seed awareness early.'),
            PlanStep(purpose='reminder', audience='All subscribers', offset_days=3, send_time='17:30',
                     message='Only a few days left — get your tickets now.',
                     rationale='Nudge fence-sitters.'),
            PlanStep(purpose='last_chance', audience='All subscribers', offset_days=0, send_time='16:00',
                     message='Doors soon! Last chance for tickets.',
                     rationale='Capture last-minute buyers.'),
        ],
    )


def _fake_structured_llm():
    """Return a MagicMock ChatOpenAI whose structured invoke returns raw/parsed/error."""
    raw = MagicMock()
    raw.usage_metadata = {'input_tokens': 120, 'output_tokens': 60, 'total_tokens': 180}

    structured = MagicMock()
    structured.invoke.return_value = {'raw': raw, 'parsed': _fake_plan(), 'parsing_error': None}

    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


@override_settings(OPENAI_API_KEY='test-key', OPENAI_MODEL='gpt-4o')
class SMSStrategistViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(
            name='Org', slug='org-strat', sms_marketing_enabled=True,
            ai_sms_strategist_enabled=True, sms_credit_balance_cents=5000,
        )
        self.user = User.objects.create_user('u', 'u@test.com', 'pw')
        UserProfile.objects.create(user=self.user, organization=self.org,
                                   org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='u@test.com', password='pw')
        self.client.get(reverse('tickets:home'))  # prime session org cache

        self.venue = Venue.objects.create(organization=self.org, name='Hall', city='Austin')
        self.event = Event.objects.create(
            organization=self.org, venue=self.venue, name='Big Show',
            start_date=date.today() + timedelta(days=20),
        )
        Customer.objects.create(organization=self.org, email='v@x.com', name='V',
                                phone='+13105550001', sms_opt_in=True, rfm_segment='VIP')

    @patch('langchain_openai.ChatOpenAI')
    def test_generate_event_plan_saves_and_meters(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        resp = self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})

        plan = SMSCampaignPlan.objects.get(organization=self.org)
        self.assertRedirects(resp, reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertEqual(plan.event_id, self.event.id)
        self.assertEqual(len(plan.steps), 3)
        # Every step carries a computed segment count + event audience criteria.
        for step in plan.steps:
            self.assertGreaterEqual(step['segments'], 1)
            self.assertEqual(step['audience_criteria'], {'event_id': str(self.event.id)})
        # Billable usage recorded under the new feature; SMS wallet untouched.
        usage = AITokenUsage.objects.get(organization=self.org)
        self.assertEqual(usage.feature, AITokenUsage.FEATURE_SMS_PLAN)
        self.assertEqual(usage.total_tokens, 180)
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 5000)

    @patch('langchain_openai.ChatOpenAI')
    def test_event_steps_have_absolute_send_dates(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)

        from datetime import datetime, timedelta
        # offset_days in _fake_plan are 14 / 3 / 0 before the event date.
        expected = {14: self.event.start_date - timedelta(days=14),
                    3: self.event.start_date - timedelta(days=3),
                    0: self.event.start_date}
        for step in plan.steps:
            self.assertNotIn('T-', step['timing_label'])          # no more relative "T-days"
            # Label carries a timezone abbreviation (e.g. "... PM PDT").
            self.assertRegex(step['timing_label'], r'[A-Z]{2,5}$')
            got = datetime.fromisoformat(step['send_at']).date()
            self.assertEqual(got, expected[step['offset_days']])

    @patch('langchain_openai.ChatOpenAI')
    def test_update_schedule_persists_and_returns_label(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)

        resp = self.client.post(
            reverse('tickets:sms_plan_update_schedule', kwargs={'pk': plan.id, 'step': 0}),
            {'send_at': '2026-07-15T09:30'},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['send_local'], '2026-07-15T09:30')
        self.assertIn('Jul 15', data['timing_label'])
        # Persisted onto the step.
        plan.refresh_from_db()
        from datetime import datetime
        self.assertEqual(datetime.fromisoformat(plan.steps[0]['send_at']).strftime('%Y-%m-%d %H:%M'),
                         '2026-07-15 09:30')
        self.assertEqual(plan.steps[0]['send_time'], '09:30')

    @patch('langchain_openai.ChatOpenAI')
    def test_org_timezone_used_in_labels(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.org.timezone = 'America/New_York'
        self.org.save(update_fields=['timezone'])
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        # Eastern time → label ends in EDT/EST, and the stored offset reflects ET.
        self.assertRegex(plan.steps[0]['timing_label'], r'E[DS]T$')
        self.assertTrue(plan.steps[0]['send_at'].endswith(('-04:00', '-05:00')))

    @patch('langchain_openai.ChatOpenAI')
    def test_launch_prefills_composer_schedule(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        # Event far enough out that all suggested sends are in the future.
        self.event.start_date = date.today() + timedelta(days=40)
        self.event.save(update_fields=['start_date'])
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)

        self.client.post(reverse('tickets:sms_plan_launch_step', kwargs={'pk': plan.id, 'step': 0}))
        prefill = self.client.session['sms_compose_prefill']
        self.assertTrue(prefill['scheduled_at'])  # a future datetime-local string
        # Composer GET pre-selects "Schedule for later" with that time.
        resp = self.client.get(reverse('tickets:sms_campaign_create'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['form'].initial.get('send_mode'),
                         resp.context['form'].SEND_SCHEDULE)
        self.assertEqual(resp.context['form'].initial.get('scheduled_at'), prefill['scheduled_at'])

    @patch('langchain_openai.ChatOpenAI')
    def test_launch_skips_past_schedule(self, mock_openai):
        from django.utils import timezone
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        # Force this step's suggested send into the past → composer schedule not prefilled.
        steps = plan.steps
        steps[0]['send_at'] = (timezone.now() - timedelta(days=2)).isoformat()
        plan.steps = steps
        plan.save(update_fields=['steps'])

        self.client.post(reverse('tickets:sms_plan_launch_step', kwargs={'pk': plan.id, 'step': 0}))
        self.assertEqual(self.client.session['sms_compose_prefill']['scheduled_at'], '')

    @patch('langchain_openai.ChatOpenAI')
    def test_update_schedule_rejects_bad_datetime(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        original = plan.steps[0]['send_at']
        resp = self.client.post(
            reverse('tickets:sms_plan_update_schedule', kwargs={'pk': plan.id, 'step': 0}),
            {'send_at': 'not-a-date'},
        )
        self.assertEqual(resp.status_code, 400)
        plan.refresh_from_db()
        self.assertEqual(plan.steps[0]['send_at'], original)

    @patch('langchain_openai.ChatOpenAI')
    def test_generate_segment_plan(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        resp = self.client.post(reverse('tickets:sms_plan_create'), {'rfm_segment': ['VIP']})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        self.assertRedirects(resp, reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertIsNone(plan.event_id)
        self.assertEqual(plan.filter_criteria, {'rfm_segment': ['VIP']})

    def test_empty_audience_is_rejected(self):
        resp = self.client.post(reverse('tickets:sms_plan_create'), {})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(SMSCampaignPlan.objects.count(), 0)

    @patch('langchain_openai.ChatOpenAI')
    def test_gated_by_flag(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.org.ai_sms_strategist_enabled = False
        self.org.save(update_fields=['ai_sms_strategist_enabled'])
        get = self.client.get(reverse('tickets:sms_plan_create'))
        self.assertEqual(get.status_code, 404)
        post = self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        self.assertEqual(post.status_code, 404)
        self.assertEqual(SMSCampaignPlan.objects.count(), 0)

    @patch('langchain_openai.ChatOpenAI')
    def test_plan_detail_org_scoped(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)

        # A different org's user cannot see this plan.
        other = Organization.objects.create(name='Other', slug='other', sms_marketing_enabled=True,
                                             ai_sms_strategist_enabled=True)
        ouser = User.objects.create_user('o', 'o@test.com', 'pw')
        UserProfile.objects.create(user=ouser, organization=other, org_role=UserProfile.OrgRole.OWNER)
        oclient = Client()
        oclient.login(username='o@test.com', password='pw')
        oclient.get(reverse('tickets:home'))
        resp = oclient.get(reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertEqual(resp.status_code, 404)

    @patch('langchain_openai.ChatOpenAI')
    def test_launch_step_prefills_composer_and_marks_launched(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)

        resp = self.client.post(
            reverse('tickets:sms_plan_launch_step', kwargs={'pk': plan.id, 'step': 0}),
        )
        # Redirects into the composer, pinned to the event.
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('tickets:sms_campaign_create'), resp.url)
        self.assertIn(f'event={self.event.id}', resp.url)
        # Session prefill carries the written body + audience.
        prefill = self.client.session['sms_compose_prefill']
        self.assertEqual(prefill['body'], plan.steps[0]['body'])
        self.assertEqual(prefill['event_id'], str(self.event.id))
        # Step is marked launched.
        plan.refresh_from_db()
        self.assertIn('launched_at', plan.steps[0])

    @patch('langchain_openai.ChatOpenAI')
    def test_launched_body_prefills_composer_form(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'rfm_segment': ['VIP']})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        self.client.post(reverse('tickets:sms_plan_launch_step', kwargs={'pk': plan.id, 'step': 0}))
        # The composer GET renders the prefilled body into the form.
        resp = self.client.get(reverse('tickets:sms_campaign_create'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, plan.steps[0]['body'])

    @patch('langchain_openai.ChatOpenAI')
    def test_update_step_persists_edit_and_returns_segments(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)

        new_body = 'Edited by the organizer — see you Friday!'
        resp = self.client.post(
            reverse('tickets:sms_plan_update_step', kwargs={'pk': plan.id, 'step': 1}),
            {'body': new_body},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertGreaterEqual(data['segments'], 1)
        # Persisted to the plan; other steps untouched.
        plan.refresh_from_db()
        self.assertEqual(plan.steps[1]['body'], new_body)
        self.assertEqual(plan.steps[1]['segments'], data['segments'])
        self.assertNotEqual(plan.steps[0]['body'], new_body)

    @patch('langchain_openai.ChatOpenAI')
    def test_update_step_rejects_empty_body(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        original = plan.steps[0]['body']
        resp = self.client.post(
            reverse('tickets:sms_plan_update_step', kwargs={'pk': plan.id, 'step': 0}),
            {'body': '   '},
        )
        self.assertEqual(resp.status_code, 400)
        plan.refresh_from_db()
        self.assertEqual(plan.steps[0]['body'], original)

    @patch('langchain_openai.ChatOpenAI')
    def test_launch_uses_edited_body_override(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)

        edited = 'Last-minute tweak before sending!'
        self.client.post(
            reverse('tickets:sms_plan_launch_step', kwargs={'pk': plan.id, 'step': 0}),
            {'body': edited},
        )
        # Override is authoritative: prefilled into the composer AND persisted.
        self.assertEqual(self.client.session['sms_compose_prefill']['body'], edited)
        plan.refresh_from_db()
        self.assertEqual(plan.steps[0]['body'], edited)
        self.assertIn('launched_at', plan.steps[0])


@override_settings(OPENAI_API_KEY='test-key', OPENAI_MODEL='gpt-4o')
class TopPriorCampaignsTests(TestCase):
    def test_prior_campaigns_org_scoped_and_handles_empty(self):
        org = Organization.objects.create(name='A', slug='a', sms_marketing_enabled=True)
        other = Organization.objects.create(name='B', slug='b', sms_marketing_enabled=True)
        self.assertEqual(_top_prior_campaigns(org), [])

        mine = SMSCampaign.objects.create(
            organization=org, name='Mine', body='hi', status=SMSCampaign.Status.SENT,
        )
        SMSMessageRecipient.objects.create(
            campaign=mine, phone='+13105550001', status=SMSMessageRecipient.Status.DELIVERED,
        )
        theirs = SMSCampaign.objects.create(
            organization=other, name='Theirs', body='yo', status=SMSCampaign.Status.SENT,
        )
        SMSMessageRecipient.objects.create(
            campaign=theirs, phone='+13105550002', status=SMSMessageRecipient.Status.DELIVERED,
        )

        rows = _top_prior_campaigns(org)
        self.assertEqual([r['body'] for r in rows], ['hi'])


@override_settings(OPENAI_API_KEY='test-key', OPENAI_MODEL='gpt-4o')
class BrandVoiceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Voice', slug='voice', sms_marketing_enabled=True)
        self.other = Organization.objects.create(name='Other', slug='voice-other', sms_marketing_enabled=True)

    def _sent(self, org, body, when):
        return SMSCampaign.objects.create(
            organization=org, name='c', body=body,
            status=SMSCampaign.Status.SENT, sent_at=when,
        )

    def test_recent_bodies_org_scoped_recent_first_deduped(self):
        from django.utils import timezone
        now = timezone.now()
        self._sent(self.org, 'oldest', now - timedelta(days=3))
        self._sent(self.org, 'newest', now - timedelta(days=1))
        self._sent(self.org, 'newest', now)                 # exact duplicate body
        self._sent(self.other, 'not mine', now)             # different org
        bodies = _recent_campaign_bodies(self.org)
        self.assertEqual(bodies, ['newest', 'oldest'])       # recency order, deduped, scoped

    @patch('langchain_openai.ChatOpenAI')
    def test_generate_passes_brand_voice_into_prompt(self, mock_openai):
        from django.utils import timezone
        distinctive = "yo fam!! doors 9pm, dont sleep on this one"
        self._sent(self.org, distinctive, timezone.now())
        llm = _fake_structured_llm()
        mock_openai.return_value = llm

        generate_campaign_plan(self.org, criteria={'all_subscribers': True}, objective='sell out')

        structured = llm.with_structured_output.return_value
        messages = structured.invoke.call_args[0][0]
        user_content = messages[1]['content']
        self.assertIn(distinctive, user_content)
        self.assertIn('brand_voice_samples', user_content)
