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
    Venue, Event, EventSMSCampaign, TrackingLink,
    TICKETING_TYPE_EXTERNAL,
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
        # Event plans default every step to all subscribers (sell to the whole list).
        for step in plan.steps:
            self.assertGreaterEqual(step['segments'], 1)
            self.assertEqual(step['audience_criteria'], {'all_subscribers': True})
            self.assertEqual(step['audience_label'], 'All SMS subscribers')
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
    def test_update_audience_to_segments(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        # Steps start targeting all subscribers.
        self.assertEqual(plan.steps[0]['audience_criteria'], {'all_subscribers': True})

        resp = self.client.post(
            reverse('tickets:sms_plan_update_audience', kwargs={'pk': plan.id, 'step': 0}),
            {'audience_mode': 'custom', 'rfm_segment': ['VIP', 'Loyal']},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertIn('VIP', data['audience_label'])
        plan.refresh_from_db()
        self.assertEqual(plan.steps[0]['audience_criteria'], {'rfm_segment': ['VIP', 'Loyal']})
        # Other steps are untouched.
        self.assertEqual(plan.steps[1]['audience_criteria'], {'all_subscribers': True})

    @patch('langchain_openai.ChatOpenAI')
    def test_update_audience_to_ticket_buyers(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        resp = self.client.post(
            reverse('tickets:sms_plan_update_audience', kwargs={'pk': plan.id, 'step': 0}),
            {'audience_mode': 'event'},
        )
        self.assertEqual(resp.status_code, 200)
        # Label uses the composer's wording ("Ticket buyers for {event}").
        self.assertEqual(resp.json()['audience_label'], f'Ticket buyers for {self.event.name}')
        plan.refresh_from_db()
        self.assertEqual(plan.steps[0]['audience_criteria'], {'event_id': str(self.event.id)})
        # Launching it opens the composer scoped to ticket buyers.
        resp = self.client.post(reverse('tickets:sms_plan_launch_step', kwargs={'pk': plan.id, 'step': 0}))
        self.assertIn('audience_scope=event', resp.url)

    @patch('langchain_openai.ChatOpenAI')
    def test_update_audience_back_to_event(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        # First narrow to a segment, then switch back to event attendees.
        self.client.post(
            reverse('tickets:sms_plan_update_audience', kwargs={'pk': plan.id, 'step': 0}),
            {'audience_mode': 'custom', 'rfm_segment': ['VIP']},
        )
        resp = self.client.post(
            reverse('tickets:sms_plan_update_audience', kwargs={'pk': plan.id, 'step': 0}),
            {'audience_mode': 'event'},
        )
        self.assertEqual(resp.status_code, 200)
        plan.refresh_from_db()
        self.assertEqual(plan.steps[0]['audience_criteria'], {'event_id': str(self.event.id)})

    @patch('langchain_openai.ChatOpenAI')
    def test_update_audience_rejects_empty_custom(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        original = plan.steps[0]['audience_criteria']
        resp = self.client.post(
            reverse('tickets:sms_plan_update_audience', kwargs={'pk': plan.id, 'step': 0}),
            {'audience_mode': 'custom'},  # nothing selected
        )
        self.assertEqual(resp.status_code, 400)
        plan.refresh_from_db()
        self.assertEqual(plan.steps[0]['audience_criteria'], original)

    @patch('langchain_openai.ChatOpenAI')
    def test_edited_audience_launches_into_composer(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        self.client.post(
            reverse('tickets:sms_plan_update_audience', kwargs={'pk': plan.id, 'step': 0}),
            {'audience_mode': 'custom', 'rfm_segment': ['VIP']},
        )
        # Launching a segment-edited step must open the composer in NON-event mode with
        # that segment selected — not fall back to the plan's event (the old bug).
        resp = self.client.post(reverse('tickets:sms_plan_launch_step', kwargs={'pk': plan.id, 'step': 0}))
        self.assertNotIn('event=', resp.url)
        prefill = self.client.session['sms_compose_prefill']
        self.assertEqual(prefill['criteria'], {'rfm_segment': ['VIP']})
        self.assertIsNone(prefill['event_id'])
        composer = self.client.get(reverse('tickets:sms_campaign_create'))
        self.assertEqual(composer.status_code, 200)
        self.assertIsNone(composer.context['event'])
        self.assertEqual(composer.context['form'].initial.get('rfm_segment'), ['VIP'])

    @patch('langchain_openai.ChatOpenAI')
    def test_unedited_event_step_launches_all_subscribers(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        # An unedited event step targets all subscribers, and its label says so — matching
        # what the composer will show (no more "All subscribers" vs "Ticket buyers" mismatch).
        self.assertEqual(plan.steps[0]['audience_label'], 'All SMS subscribers')
        # Launch keeps the event link (for attribution) but opens on the All-subscribers scope.
        resp = self.client.post(reverse('tickets:sms_plan_launch_step', kwargs={'pk': plan.id, 'step': 0}))
        self.assertIn(f'event={self.event.id}', resp.url)
        self.assertIn('audience_scope=all', resp.url)
        # The composer renders that scope selected.
        composer = self.client.get(f"{reverse('tickets:sms_campaign_create')}?event={self.event.id}&audience_scope=all")
        self.assertEqual(composer.status_code, 200)
        self.assertEqual(composer.context['audience_scope'], 'all')

    @patch('langchain_openai.ChatOpenAI')
    def test_remove_step_drops_it_and_reindexes(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        self.assertEqual(len(plan.steps), 3)
        middle_body = plan.steps[1]['body']
        last_body = plan.steps[2]['body']

        resp = self.client.post(
            reverse('tickets:sms_plan_remove_step', kwargs={'pk': plan.id, 'step': 1}),
        )
        self.assertRedirects(resp, reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        plan.refresh_from_db()
        # The middle message is gone; the last one shifts up and orders stay 0..n-1.
        self.assertEqual(len(plan.steps), 2)
        bodies = [s['body'] for s in plan.steps]
        self.assertNotIn(middle_body, bodies)
        self.assertEqual(plan.steps[1]['body'], last_body)
        self.assertEqual([s['order'] for s in plan.steps], [0, 1])

    @patch('langchain_openai.ChatOpenAI')
    def test_remove_step_org_scoped_and_gated(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)

        # Gated off → 404, plan untouched.
        self.org.ai_sms_strategist_enabled = False
        self.org.save(update_fields=['ai_sms_strategist_enabled'])
        resp = self.client.post(
            reverse('tickets:sms_plan_remove_step', kwargs={'pk': plan.id, 'step': 0}),
        )
        self.assertEqual(resp.status_code, 404)
        plan.refresh_from_db()
        self.assertEqual(len(plan.steps), 3)

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

    def _slicktext(self, org, message, when, **kwargs):
        venue = Venue.objects.create(organization=org, name='V', city='A')
        event = Event.objects.create(organization=org, venue=venue, name='Show',
                                     start_date=date.today())
        return EventSMSCampaign.objects.create(
            event=event, source='slicktext', external_id=message[:20],
            name='ext', message=message, send_time=when, **kwargs,
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

    def test_recent_bodies_include_slicktext_history(self):
        from django.utils import timezone
        now = timezone.now()
        self._sent(self.org, 'native older', now - timedelta(days=5))
        self._slicktext(self.org, 'hii we outside tmrw?? lock in!', now - timedelta(days=1))
        self._slicktext(self.other, 'not my slicktext', now)   # different org
        bodies = _recent_campaign_bodies(self.org)
        # SlickText message is included and (being more recent) ranks first; org-scoped.
        self.assertEqual(bodies[0], 'hii we outside tmrw?? lock in!')
        self.assertIn('native older', bodies)
        self.assertNotIn('not my slicktext', bodies)

    def test_top_prior_includes_slicktext(self):
        from django.utils import timezone
        self._slicktext(self.org, 'slick winner', timezone.now(),
                        audience_size=1000, unique_clicks=300, orders=5)
        rows = _top_prior_campaigns(self.org)
        bodies = [r['body'] for r in rows]
        self.assertIn('slick winner', bodies)

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


@override_settings(SITE_URL='https://cue.test')
class ExternalTicketLinkTests(TestCase):
    """Imported (external) events can include a trackable ticket link in plan messages."""

    def setUp(self):
        self.org = Organization.objects.create(name='Ext', slug='ext', sms_marketing_enabled=True)
        self.venue = Venue.objects.create(organization=self.org, name='V', city='A')
        self.event = Event.objects.create(
            organization=self.org, venue=self.venue, name='Imported Show',
            start_date=date.today() + timedelta(days=10),
            ticketing_type=TICKETING_TYPE_EXTERNAL, ticket_link='https://tix.example.com/e/123',
        )

    def test_event_ticket_url_creates_targeted_link(self):
        from tickets.sms_views import _event_ticket_url
        url = _event_ticket_url(None, self.org, self.event)
        self.assertTrue(url.startswith('https://cue.test/t/'))
        link = TrackingLink.objects.get(organization=self.org, event=self.event, name='SMS')
        self.assertEqual(link.target_url, 'https://tix.example.com/e/123')
        self.assertIn(link.token, url)

    def test_event_ticket_url_refreshes_target_when_link_changes(self):
        from tickets.sms_views import _event_ticket_url
        _event_ticket_url(None, self.org, self.event)
        self.event.ticket_link = 'https://tix.example.com/e/999'
        self.event.save(update_fields=['ticket_link'])
        _event_ticket_url(None, self.org, self.event)
        link = TrackingLink.objects.get(organization=self.org, event=self.event, name='SMS')
        self.assertEqual(link.target_url, 'https://tix.example.com/e/999')

    def test_external_event_without_link_returns_empty(self):
        from tickets.sms_views import _event_ticket_url
        self.event.ticket_link = ''
        self.event.save(update_fields=['ticket_link'])
        self.assertEqual(_event_ticket_url(None, self.org, self.event), '')

    def test_redirect_records_click_and_forwards_offsite(self):
        link = TrackingLink.objects.create(
            organization=self.org, event=self.event, name='SMS',
            token='exttok123456', target_url='https://tix.example.com/e/123',
        )
        resp = Client().get(reverse('tickets:track_link_redirect', kwargs={'token': link.token}))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, 'https://tix.example.com/e/123')
        link.refresh_from_db()
        self.assertEqual(link.click_count, 1)

    def test_campaign_detail_shows_external_hint_and_hides_revenue(self):
        user = User.objects.create_user('h', 'h@test.com', 'pw')
        UserProfile.objects.create(user=user, organization=self.org,
                                   org_role=UserProfile.OrgRole.OWNER)
        c = Client()
        c.login(username='h@test.com', password='pw')
        c.get(reverse('tickets:home'))
        link = TrackingLink.objects.create(
            organization=self.org, event=self.event, name='SMS',
            token='dettok123456', target_url='https://tix.example.com/e/123',
        )
        campaign = SMSCampaign.objects.create(
            organization=self.org, name='C', body='Grab tix https://cue.test/t/dettok123456/',
            link_url='https://cue.test/t/dettok123456/', status=SMSCampaign.Status.SENT,
        )
        resp = c.get(reverse('tickets:sms_campaign_detail', kwargs={'pk': campaign.id}))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['external_ticket_link'])
        self.assertContains(resp, 'ticket sales and revenue are not')
        self.assertNotContains(resp, 'Tickets bought')   # revenue/ticket cards suppressed

    def test_mint_campaign_link_preserves_target(self):
        from tickets.sms_views import _mint_campaign_tracking_link
        src = TrackingLink.objects.create(
            organization=self.org, event=self.event, name='SMS',
            token='srctok123456', target_url='https://tix.example.com/e/123',
        )
        campaign = SMSCampaign(
            organization=self.org, name='C',
            body='Grab tix https://cue.test/t/srctok123456/',
            link_url='https://cue.test/t/srctok123456/',
        )
        _mint_campaign_tracking_link(self.org, campaign)
        # A fresh per-campaign link was minted and still redirects off-site.
        minted = TrackingLink.objects.exclude(pk=src.pk).get(organization=self.org)
        self.assertEqual(minted.target_url, 'https://tix.example.com/e/123')
        self.assertIn(minted.token, campaign.link_url)

    @override_settings(OPENAI_API_KEY='test-key', OPENAI_MODEL='gpt-4o')
    @patch('langchain_openai.ChatOpenAI')
    def test_plan_generation_threads_ticket_link_into_prompt(self, mock_openai):
        llm = _fake_structured_llm()
        mock_openai.return_value = llm
        self.org.ai_sms_strategist_enabled = True
        self.org.save(update_fields=['ai_sms_strategist_enabled'])
        user = User.objects.create_user('e', 'e@test.com', 'pw')
        UserProfile.objects.create(user=user, organization=self.org,
                                   org_role=UserProfile.OrgRole.OWNER)
        c = Client()
        c.login(username='e@test.com', password='pw')
        c.get(reverse('tickets:home'))
        c.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})

        user_content = llm.with_structured_output.return_value.invoke.call_args[0][0][1]['content']
        link = TrackingLink.objects.get(organization=self.org, event=self.event, name='SMS')
        self.assertIn(f'cue.test/t/{link.token}/', user_content)
