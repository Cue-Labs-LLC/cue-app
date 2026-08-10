"""Tests for the AI SMS Campaign Strategist: plan generation, gating, org scoping,
token metering, and launching a step into the composer."""

import uuid
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    Organization, UserProfile, Customer, CustomerTag,
    SMSCampaign, SMSCampaignPlan, SMSMessageRecipient, AITokenUsage,
    Venue, Event, EventSMSCampaign, TrackingLink, Market,
    MARKET_GEOGRAPHY_CITY,
    TICKETING_TYPE_EXTERNAL,
)
from .services.sms_strategist import (
    CampaignPlan, PlanStep, RegeneratedMessage, generate_campaign_plan,
    _top_prior_campaigns, _recent_campaign_bodies,
)
from .sms_views import _plan_step_event, _plan_progress


def _fake_plan():
    return CampaignPlan(
        title='LA sellout sprint',
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


def _fake_regen_llm(message='A brand new take on this message. Grab tix: https://cue.test/t/abc/',
                    rationale='Fresh angle for this touch.'):
    """MagicMock ChatOpenAI whose structured invoke returns a single RegeneratedMessage."""
    raw = MagicMock()
    raw.usage_metadata = {'input_tokens': 40, 'output_tokens': 20, 'total_tokens': 60}

    structured = MagicMock()
    structured.invoke.return_value = {
        'raw': raw,
        'parsed': RegeneratedMessage(message=message, rationale=rationale),
        'parsing_error': None,
    }

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

    def _gen(self, data=None):
        """Generate a plan (stashes an unsaved preview) then Save-as-draft, so a persisted
        SMSCampaignPlan exists — mirroring the old one-shot 'generate persists' flow most of
        these tests were written against. Returns the *generate* response."""
        resp = self.client.post(reverse('tickets:sms_plan_create'), data or {})
        self.client.post(reverse('tickets:sms_plan_save'))
        return resp

    @patch('langchain_openai.ChatOpenAI')
    def test_generate_event_plan_saves_and_meters(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        create_url = reverse('tickets:sms_plan_create')
        # Generating alone persists NOTHING — it stashes an unsaved preview and redirects there.
        resp = self.client.post(create_url, {'event': str(self.event.id)})
        self.assertRedirects(resp, reverse('tickets:sms_plan_preview'))
        self.assertEqual(SMSCampaignPlan.objects.count(), 0)
        # ...but token metering still happens at generation time (the LLM already ran).
        usage = AITokenUsage.objects.get(organization=self.org)
        self.assertEqual(usage.feature, AITokenUsage.FEATURE_SMS_PLAN)
        self.assertEqual(usage.total_tokens, 180)
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 5000)

        # Saving as a draft is what actually creates the plan row.
        save = self.client.post(reverse('tickets:sms_plan_save'))
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        self.assertRedirects(save, reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertEqual(plan.event_id, self.event.id)
        # The plan is named by the AI's distinctive title, not the generic "Plan · {event}".
        self.assertEqual(plan.name, 'LA sellout sprint')
        self.assertEqual(len(plan.steps), 3)
        # Event plans default every step to all subscribers (sell to the whole list).
        for step in plan.steps:
            self.assertGreaterEqual(step['segments'], 1)
            self.assertEqual(step['audience_criteria'], {'all_subscribers': True})
            self.assertEqual(step['audience_label'], 'All SMS subscribers')

    @patch('langchain_openai.ChatOpenAI')
    def test_blank_ai_title_falls_back_to_event_name(self, mock_openai):
        # If the model returns no usable title, the plan keeps the generic "Plan · {event}".
        plan_obj = _fake_plan()
        plan_obj.title = '   '
        structured = MagicMock()
        structured.invoke.return_value = {
            'raw': MagicMock(usage_metadata={'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}),
            'parsed': plan_obj, 'parsing_error': None,
        }
        llm = MagicMock()
        llm.with_structured_output.return_value = structured
        mock_openai.return_value = llm

        self._gen({'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        self.assertEqual(plan.name, f'Plan · {self.event.name}')

    @patch('langchain_openai.ChatOpenAI')
    def test_event_plan_prioritizes_market_matching_venue_city(self, mock_openai):
        # A city-level market covering the venue's city should be chosen over all subscribers.
        market = Market.objects.create(
            organization=self.org, name='Austin', geography_level=MARKET_GEOGRAPHY_CITY,
            geography_value='Austin',
        )
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})

        plan = SMSCampaignPlan.objects.get(organization=self.org)
        for step in plan.steps:
            self.assertEqual(step['audience_criteria'], {'market_ids': [str(market.id)]})
            self.assertEqual(step['audience_label'], 'Markets: Austin')

    @patch('langchain_openai.ChatOpenAI')
    def test_event_plan_falls_back_to_all_subscribers_without_matching_market(self, mock_openai):
        # A market in a different city must not be chosen for this venue.
        Market.objects.create(
            organization=self.org, name='Seattle', geography_level=MARKET_GEOGRAPHY_CITY,
            geography_value='Seattle',
        )
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})

        plan = SMSCampaignPlan.objects.get(organization=self.org)
        for step in plan.steps:
            self.assertEqual(step['audience_criteria'], {'all_subscribers': True})
            self.assertEqual(step['audience_label'], 'All SMS subscribers')

    @patch('langchain_openai.ChatOpenAI')
    def test_event_steps_have_absolute_send_dates(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
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

    def test_step_send_time_never_in_the_past(self):
        # A touch anchored on an event happening today, at a send time that has already
        # passed, must be nudged into the future — never scheduled in the past.
        from datetime import datetime
        from django.utils import timezone
        from .services.sms_strategist import _compute_step_schedule

        tz = self.org.get_timezone()
        now = timezone.now().astimezone(tz)
        past_time = (now - timedelta(hours=2)).strftime('%H:%M')
        step = PlanStep(purpose='announcement', audience='All subscribers', offset_days=0,
                        send_time=past_time, message='Doors soon!', rationale='r')
        self.event.start_date = now.date()

        iso, label = _compute_step_schedule(step, self.event, tz)
        self.assertGreater(datetime.fromisoformat(iso), now)

    def test_step_send_time_never_after_event_start(self):
        # A day-of touch whose send time falls after the event's start time must be pulled
        # back to before doors — a "doors open soon" text can't go out after doors.
        from datetime import datetime, time as dtime, timedelta as td
        from django.utils import timezone
        from .services.sms_strategist import _compute_step_schedule, EVENT_START_LEAD_MINUTES

        tz = self.org.get_timezone()
        now = timezone.now().astimezone(tz)
        # Event is a few days out at 3:00 PM local; the model picked a 4:00 PM send.
        self.event.start_date = (now + td(days=3)).date()
        self.event.start_time = dtime(15, 0)
        step = PlanStep(purpose='last_chance', audience='All subscribers', offset_days=0,
                        send_time='16:00', message='Doors open soon!', rationale='r')

        iso, label = _compute_step_schedule(step, self.event, tz)
        scheduled = datetime.fromisoformat(iso)
        event_start = datetime.combine(self.event.start_date, dtime(15, 0), tzinfo=tz)
        self.assertLess(scheduled, event_start)
        # Pulled back to the configured lead before doors.
        self.assertEqual(scheduled, event_start - td(minutes=EVENT_START_LEAD_MINUTES))

    @patch('langchain_openai.ChatOpenAI')
    def test_update_schedule_persists_and_returns_label(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
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
        self._gen({'event': str(self.event.id)})
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
        self._gen({'event': str(self.event.id)})
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
        self._gen({'event': str(self.event.id)})
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
        self._gen({'event': str(self.event.id)})
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
        self._gen({'event': str(self.event.id)})
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
        self._gen({'event': str(self.event.id)})
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
        self._gen({'event': str(self.event.id)})
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
        self._gen({'event': str(self.event.id)})
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
        self._gen({'event': str(self.event.id)})
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
        self._gen({'event': str(self.event.id)})
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
    def test_remove_step_blocked_once_sent(self, mock_openai):
        # A message that's already been sent can't be removed from the plan.
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        campaign = SMSCampaign.objects.create(
            organization=self.org, name='sent', body=plan.steps[0]['body'],
            status=SMSCampaign.Status.SENT)
        plan.steps[0]['launched_campaign_id'] = str(campaign.id)
        plan.steps[0]['launched_at'] = timezone.now().isoformat()
        plan.save(update_fields=['steps'])

        resp = self.client.post(
            reverse('tickets:sms_plan_remove_step', kwargs={'pk': plan.id, 'step': 0}),
        )
        self.assertRedirects(resp, reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        plan.refresh_from_db()
        # Still there — the sent step was not dropped.
        self.assertEqual(len(plan.steps), 3)
        self.assertEqual(plan.steps[0]['launched_campaign_id'], str(campaign.id))
        # And the detail page hides its trash button for that step.
        detail = self.client.get(reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertTrue(detail.context['steps'][0]['is_sent'])
        self.assertFalse(detail.context['steps'][1].get('is_sent'))

    def _plan_with_sent_first_step(self):
        """Generate a plan and mark step 0 as SENT (steps 1-2 stay draft). Returns
        (plan, campaign) for the edit-lock tests below."""
        self._gen({'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        campaign = SMSCampaign.objects.create(
            organization=self.org, name='sent', body=plan.steps[0]['body'],
            status=SMSCampaign.Status.SENT)
        plan.steps[0]['launched_campaign_id'] = str(campaign.id)
        plan.steps[0]['launched_at'] = timezone.now().isoformat()
        plan.save(update_fields=['steps'])
        return plan, campaign

    @patch('langchain_openai.ChatOpenAI')
    def test_update_step_blocked_once_sent(self, mock_openai):
        # The message text of a sent step can't be edited.
        mock_openai.return_value = _fake_structured_llm()
        plan, _ = self._plan_with_sent_first_step()
        original = plan.steps[0]['body']

        resp = self.client.post(
            reverse('tickets:sms_plan_update_step', kwargs={'pk': plan.id, 'step': 0}),
            {'body': 'Rewritten after the fact'},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.json()['ok'])
        plan.refresh_from_db()
        self.assertEqual(plan.steps[0]['body'], original)

    @patch('langchain_openai.ChatOpenAI')
    def test_update_schedule_blocked_once_sent(self, mock_openai):
        # The send time of a sent step can't be edited.
        mock_openai.return_value = _fake_structured_llm()
        plan, _ = self._plan_with_sent_first_step()
        original = plan.steps[0]['send_at']

        resp = self.client.post(
            reverse('tickets:sms_plan_update_schedule', kwargs={'pk': plan.id, 'step': 0}),
            {'send_at': '2026-07-15T09:30'},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.json()['ok'])
        plan.refresh_from_db()
        self.assertEqual(plan.steps[0]['send_at'], original)

    @patch('langchain_openai.ChatOpenAI')
    def test_update_audience_blocked_once_sent(self, mock_openai):
        # The audience of a sent step can't be edited.
        mock_openai.return_value = _fake_structured_llm()
        plan, _ = self._plan_with_sent_first_step()
        self.assertEqual(plan.steps[0]['audience_criteria'], {'all_subscribers': True})

        resp = self.client.post(
            reverse('tickets:sms_plan_update_audience', kwargs={'pk': plan.id, 'step': 0}),
            {'audience_mode': 'custom', 'rfm_segment': ['VIP']},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.json()['ok'])
        plan.refresh_from_db()
        self.assertEqual(plan.steps[0]['audience_criteria'], {'all_subscribers': True})

    @patch('langchain_openai.ChatOpenAI')
    def test_launch_step_ignores_body_override_once_sent(self, mock_openai):
        # Opening the composer for a sent step must not rewrite its stored body.
        mock_openai.return_value = _fake_structured_llm()
        plan, _ = self._plan_with_sent_first_step()
        original = plan.steps[0]['body']

        resp = self.client.post(
            reverse('tickets:sms_plan_launch_step', kwargs={'pk': plan.id, 'step': 0}),
            {'body': 'hacked'},
        )
        # Launch still functions (redirects to the composer); only the persist is suppressed.
        self.assertEqual(resp.status_code, 302)
        plan.refresh_from_db()
        self.assertEqual(plan.steps[0]['body'], original)

    @patch('langchain_openai.ChatOpenAI')
    def test_edit_endpoints_still_work_on_unsent_step(self, mock_openai):
        # The lock is per-step: a draft step in a plan that also has a sent step still edits.
        mock_openai.return_value = _fake_structured_llm()
        plan, _ = self._plan_with_sent_first_step()

        # Body
        resp = self.client.post(
            reverse('tickets:sms_plan_update_step', kwargs={'pk': plan.id, 'step': 1}),
            {'body': 'Fresh draft copy for touch two.'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['ok'])
        # Schedule
        resp = self.client.post(
            reverse('tickets:sms_plan_update_schedule', kwargs={'pk': plan.id, 'step': 1}),
            {'send_at': '2026-07-15T09:30'},
        )
        self.assertEqual(resp.status_code, 200)
        # Audience
        resp = self.client.post(
            reverse('tickets:sms_plan_update_audience', kwargs={'pk': plan.id, 'step': 1}),
            {'audience_mode': 'custom', 'rfm_segment': ['VIP']},
        )
        self.assertEqual(resp.status_code, 200)

        plan.refresh_from_db()
        self.assertEqual(plan.steps[1]['body'], 'Fresh draft copy for touch two.')
        self.assertEqual(plan.steps[1]['send_time'], '09:30')
        self.assertEqual(plan.steps[1]['audience_criteria'].get('rfm_segment'), ['VIP'])

    @patch('langchain_openai.ChatOpenAI')
    def test_sent_step_render_omits_edit_affordances(self, mock_openai):
        # The detail page drops the edit affordances for a sent step but keeps them for drafts.
        mock_openai.return_value = _fake_structured_llm()
        plan, _ = self._plan_with_sent_first_step()

        resp = self.client.get(reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()

        # Sent step (0): no per-step edit URLs; static, read-only message bubble.
        for name in ('sms_plan_update_step', 'sms_plan_update_schedule', 'sms_plan_update_audience'):
            self.assertNotIn(reverse(f'tickets:{name}', kwargs={'pk': plan.id, 'step': 0}), html)
        self.assertIn('plan-step-message-sent', html)
        # Draft step (1): all three edit URLs still present.
        for name in ('sms_plan_update_step', 'sms_plan_update_schedule', 'sms_plan_update_audience'):
            self.assertIn(reverse(f'tickets:{name}', kwargs={'pk': plan.id, 'step': 1}), html)

        self.assertTrue(resp.context['steps'][0]['is_sent'])
        self.assertFalse(resp.context['steps'][1].get('is_sent'))

    # --- Add a message after a sent step ---------------------------------------

    @patch('langchain_openai.ChatOpenAI')
    def test_add_step_after_sent_inserts_blank_draft_and_reindexes(self, mock_openai):
        # Inserting after a sent step drops a fresh blank draft at that position and keeps
        # order == index across the whole sequence.
        mock_openai.return_value = _fake_structured_llm()
        plan, campaign = self._plan_with_sent_first_step()
        second_body = plan.steps[1]['body']

        resp = self.client.post(
            reverse('tickets:sms_plan_add_step_after', kwargs={'pk': plan.id, 'step': 0}),
        )
        self.assertRedirects(resp, reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        plan.refresh_from_db()

        self.assertEqual(len(plan.steps), 4)
        new = plan.steps[1]
        self.assertEqual(new['body'], '')
        self.assertIsNone(new['launched_campaign_id'])
        self.assertEqual(new['purpose'], 'follow_up')
        # Inherits the plan's default audience (event plan with no matching market → all subs).
        self.assertEqual(new['audience_criteria'], {'all_subscribers': True})
        self.assertGreaterEqual(new['segments'], 1)
        # The old second step shifted down; every order matches its index.
        self.assertEqual(plan.steps[2]['body'], second_body)
        self.assertEqual([s['order'] for s in plan.steps], [0, 1, 2, 3])
        # The sent step is untouched.
        self.assertEqual(plan.steps[0]['launched_campaign_id'], str(campaign.id))

    @patch('langchain_openai.ChatOpenAI')
    def test_add_step_after_any_step_inserts(self, mock_openai):
        # The connector "+" works on any message (all-draft plans included), not just sent ones,
        # and carries the body composed in the modal.
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)  # 3 draft steps
        first_body = plan.steps[0]['body']

        resp = self.client.post(
            reverse('tickets:sms_plan_add_step_after', kwargs={'pk': plan.id, 'step': 0}),
            {'body': 'A fresh mid-sequence nudge.'},
        )
        self.assertRedirects(resp, reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        plan.refresh_from_db()
        self.assertEqual(len(plan.steps), 4)  # inserted after the draft step
        self.assertEqual(plan.steps[0]['body'], first_body)  # existing step untouched
        self.assertEqual(plan.steps[1]['body'], 'A fresh mid-sequence nudge.')  # composed body
        self.assertGreaterEqual(plan.steps[1]['segments'], 1)
        self.assertEqual([s['order'] for s in plan.steps], [0, 1, 2, 3])

    @patch('langchain_openai.ChatOpenAI')
    def test_add_message_with_audience_and_schedule(self, mock_openai):
        # The modal can set a custom audience + a send time; both land on the new step.
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)

        resp = self.client.post(
            reverse('tickets:sms_plan_add_step_after', kwargs={'pk': plan.id, 'step': 1}),
            {'body': 'VIPs only, act now.', 'audience_mode': 'custom',
             'rfm_segment': ['VIP'], 'send_at': '2026-09-15T18:30'},
        )
        self.assertRedirects(resp, reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        plan.refresh_from_db()
        new = plan.steps[2]  # inserted after step 1
        self.assertEqual(new['body'], 'VIPs only, act now.')
        self.assertEqual(new['audience_criteria'].get('rfm_segment'), ['VIP'])
        self.assertEqual(new['send_time'], '18:30')
        self.assertTrue(new['send_at'])

    @patch('langchain_openai.ChatOpenAI')
    def test_add_message_modal_prefills_plan_market(self, mock_openai):
        # The Add-a-message modal opens pre-filled with the plan's own audience: an event plan
        # whose venue matches a Market defaults to that market (normalized to a single market_id).
        mock_openai.return_value = _fake_structured_llm()
        market = Market.objects.create(
            organization=self.org, name='Austin', geography_level=MARKET_GEOGRAPHY_CITY,
            geography_value='Austin',
        )
        plan = self._make_event_plan()

        resp = self.client.get(reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertEqual(resp.context['add_default_criteria'], {'market_id': str(market.id)})
        html = resp.content.decode()
        self.assertIn('add-msg-default-audience', html)   # the prefill JSON is embedded
        self.assertIn(str(market.id), html)

    @patch('langchain_openai.ChatOpenAI')
    def test_plan_draft_message_returns_ai_body(self, mock_openai):
        # "Generate with AI" in the modal drafts a message from the plan context (no persist).
        mock_openai.return_value = _fake_structured_llm()   # plan generation uses the plan LLM
        self._gen({'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        before = len(plan.steps)

        # Now the single-message drafter returns a RegeneratedMessage.
        mock_openai.return_value = _fake_regen_llm(message='Generated follow-up. Grab tix!')
        resp = self.client.post(
            reverse('tickets:sms_plan_draft_message', kwargs={'pk': plan.id}),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['body'], 'Generated follow-up. Grab tix!')
        self.assertGreaterEqual(data['segments'], 1)
        # It also suggests a send time (datetime-local) for the modal's schedule field: a valid,
        # future, parseable slot.
        from datetime import datetime
        self.assertIn('send_local', data)
        suggested = datetime.strptime(data['send_local'], '%Y-%m-%dT%H:%M')
        self.assertGreater(suggested, datetime.now())
        # Drafting does not add a step.
        plan.refresh_from_db()
        self.assertEqual(len(plan.steps), before)

    @patch('langchain_openai.ChatOpenAI')
    def test_draft_message_suggests_time_between_neighbors(self, mock_openai):
        # Opening the "+" between two scheduled messages suggests a time in that gap.
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        mock_openai.return_value = _fake_regen_llm()

        from datetime import datetime
        tz = self.org.get_timezone()
        # Steps 0 and 1 are scheduled ~14 and ~3 days before the event (both future).
        before = datetime.fromisoformat(plan.steps[0]['send_at']).astimezone(tz).replace(tzinfo=None)
        after = datetime.fromisoformat(plan.steps[1]['send_at']).astimezone(tz).replace(tzinfo=None)
        self.assertLess(before, after)

        resp = self.client.post(
            reverse('tickets:sms_plan_draft_message', kwargs={'pk': plan.id}),
            {'after_step': '0'},   # "+" between step 0 and step 1
        )
        data = resp.json()
        suggested = datetime.strptime(data['send_local'], '%Y-%m-%dT%H:%M')
        self.assertLess(before, suggested)
        self.assertLess(suggested, after)

    # --- Confirm & schedule all -----------------------------------------------

    @patch('langchain_openai.ChatOpenAI')
    def test_confirm_all_schedules_every_draft(self, mock_openai):
        # One action schedules every draft message: a campaign per step, each step stamped.
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)  # 3 future-timed drafts

        resp = self.client.post(reverse('tickets:sms_plan_confirm_all', kwargs={'pk': plan.id}))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['scheduled'], 3)
        self.assertEqual(data['skipped'], [])
        self.assertEqual(SMSCampaign.objects.filter(organization=self.org).count(), 3)
        plan.refresh_from_db()
        self.assertTrue(all(s['launched_campaign_id'] for s in plan.steps))
        self.assertEqual(plan.status, SMSCampaignPlan.Status.SCHEDULED)

    @patch('langchain_openai.ChatOpenAI')
    def test_confirm_all_skips_not_ready(self, mock_openai):
        # A blank-body message is skipped (and reported); the ready ones still schedule.
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        # Append a blank draft (no body posted → blank).
        self.client.post(
            reverse('tickets:sms_plan_add_step_after', kwargs={'pk': plan.id, 'step': 2}),
        )
        plan.refresh_from_db()
        self.assertEqual(len(plan.steps), 4)

        resp = self.client.post(reverse('tickets:sms_plan_confirm_all', kwargs={'pk': plan.id}))
        data = resp.json()
        self.assertEqual(data['scheduled'], 3)
        self.assertEqual(len(data['skipped']), 1)
        self.assertIn('no message text', data['skipped'][0]['reason'])
        plan.refresh_from_db()
        # The blank step (index 3) stays unlaunched; the three real ones are launched.
        self.assertIsNone(plan.steps[3]['launched_campaign_id'])
        self.assertEqual(SMSCampaign.objects.filter(organization=self.org).count(), 3)

    @patch('langchain_openai.ChatOpenAI')
    def test_confirm_all_blocked_when_disabled(self, mock_openai):
        # A disabled (paused) plan can't bulk-send — 409, nothing scheduled.
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        plan.enabled = False
        plan.save(update_fields=['enabled'])

        resp = self.client.post(reverse('tickets:sms_plan_confirm_all', kwargs={'pk': plan.id}))
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.json()['ok'])
        self.assertEqual(SMSCampaign.objects.filter(organization=self.org).count(), 0)

    @patch('langchain_openai.ChatOpenAI')
    def test_preview_all_returns_totals_and_not_ready(self, mock_openai):
        # The preview sums recipients/cost across the ready drafts and lists the not-ready ones.
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        self.client.post(
            reverse('tickets:sms_plan_add_step_after', kwargs={'pk': plan.id, 'step': 2}),
        )  # a blank (not-ready) step

        resp = self.client.post(reverse('tickets:sms_plan_preview_all', kwargs={'pk': plan.id}))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['ready_count'], 3)          # the three AI messages
        self.assertEqual(data['total_recipients'], 3)     # one opted-in customer × 3
        self.assertGreaterEqual(data['total_cost_tokens'], 3)
        self.assertEqual(len(data['not_ready']), 1)
        self.assertIn('no message text', data['not_ready'][0]['reason'])

    @patch('langchain_openai.ChatOpenAI')
    def test_confirm_all_button_renders_with_drafts(self, mock_openai):
        # The header exposes "Confirm & schedule all" while drafts remain; gone once all launched.
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)

        html = self.client.get(
            reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id})).content.decode()
        self.assertIn('id="planConfirmAllBtn"', html)
        self.assertIn('id="confirmAllModal"', html)

        # Launch every step → the button disappears.
        for i, step in enumerate(plan.steps):
            c = SMSCampaign.objects.create(
                organization=self.org, name=f'c{i}', body=step['body'],
                status=SMSCampaign.Status.SCHEDULED)
            step['launched_campaign_id'] = str(c.id)
        plan.save(update_fields=['steps'])
        html = self.client.get(
            reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id})).content.decode()
        self.assertNotIn('id="planConfirmAllBtn"', html)

    @patch('langchain_openai.ChatOpenAI')
    def test_add_step_reverts_fully_sent_plan_to_in_progress(self, mock_openai):
        # A completed (all-sent) plan gains a draft → status re-derives to In progress.
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        for i, step in enumerate(plan.steps):
            c = SMSCampaign.objects.create(
                organization=self.org, name=f'sent{i}', body=step['body'],
                status=SMSCampaign.Status.SENT)
            step['launched_campaign_id'] = str(c.id)
            step['launched_at'] = timezone.now().isoformat()
        plan.status = SMSCampaignPlan.Status.SENT
        plan.save(update_fields=['steps', 'status'])

        self.client.post(
            reverse('tickets:sms_plan_add_step_after', kwargs={'pk': plan.id, 'step': 2}),
        )
        plan.refresh_from_db()
        self.assertEqual(len(plan.steps), 4)
        self.assertEqual(plan.status, SMSCampaignPlan.Status.IN_PROGRESS)

    @patch('langchain_openai.ChatOpenAI')
    def test_added_step_is_editable_and_sendable(self, mock_openai):
        # The new blank step behaves like any draft: rejects an empty-body send, then sends
        # once written, stamping launched_campaign_id.
        mock_openai.return_value = _fake_structured_llm()
        plan, _ = self._plan_with_sent_first_step()
        self.client.post(
            reverse('tickets:sms_plan_add_step_after', kwargs={'pk': plan.id, 'step': 0}),
        )
        new_index = 1  # inserted right after the sent step

        # Empty body → confirm refuses.
        resp = self.client.post(
            reverse('tickets:sms_plan_confirm_step', kwargs={'pk': plan.id, 'step': new_index}),
            {'idempotency_key': 'k1'},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()['ok'])

        # Write a body via the inline editor, then send.
        resp = self.client.post(
            reverse('tickets:sms_plan_update_step', kwargs={'pk': plan.id, 'step': new_index}),
            {'body': 'One more reason to grab tickets before Friday.'},
        )
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(
            reverse('tickets:sms_plan_confirm_step', kwargs={'pk': plan.id, 'step': new_index}),
            {'idempotency_key': 'k2'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['ok'])
        plan.refresh_from_db()
        self.assertIsNotNone(plan.steps[new_index]['launched_campaign_id'])

    @patch('langchain_openai.ChatOpenAI')
    def test_add_step_after_org_scoped_and_gated(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan, _ = self._plan_with_sent_first_step()

        # Gated off → 404, nothing added.
        self.org.ai_sms_strategist_enabled = False
        self.org.save(update_fields=['ai_sms_strategist_enabled'])
        resp = self.client.post(
            reverse('tickets:sms_plan_add_step_after', kwargs={'pk': plan.id, 'step': 0}),
        )
        self.assertEqual(resp.status_code, 404)
        self.org.ai_sms_strategist_enabled = True
        self.org.save(update_fields=['ai_sms_strategist_enabled'])
        plan.refresh_from_db()
        self.assertEqual(len(plan.steps), 3)

        # GET is rejected (POST-only).
        resp = self.client.get(
            reverse('tickets:sms_plan_add_step_after', kwargs={'pk': plan.id, 'step': 0}),
        )
        self.assertEqual(resp.status_code, 405)

    @patch('langchain_openai.ChatOpenAI')
    def test_add_message_connector_opens_modal(self, mock_openai):
        # Each step's connector "+" is a modal trigger (data-add-message) carrying its insert
        # URL; the "Add a message" modal exists; the old inline divider/connector form is gone.
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)  # 3 draft steps

        html = self.client.get(
            reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id})).content.decode()
        self.assertIn('data-add-message', html)
        self.assertIn('id="addMessageModal"', html)
        self.assertIn('id="addMessageForm"', html)
        # An explicit "Add message" button appends at the end of the list.
        self.assertIn('plan-add-bottom', html)
        self.assertNotIn('plan-add-here', html)
        # The connector's dev comment must not leak into the page (multi-line {# #} pitfall).
        self.assertNotIn('Hover the connector line', html)
        for i in range(len(plan.steps)):
            self.assertIn(
                reverse('tickets:sms_plan_add_step_after', kwargs={'pk': plan.id, 'step': i}), html)

    @patch('langchain_openai.ChatOpenAI')
    def test_remove_step_org_scoped_and_gated(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
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
    def test_regenerate_step_replaces_body_and_rationale(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        old_body = plan.steps[0]['body']
        # Preserve the step's schedule/audience across a regenerate.
        old_send_at = plan.steps[0]['send_at']
        old_audience = plan.steps[0]['audience_criteria']

        mock_openai.return_value = _fake_regen_llm(message='Totally fresh copy for touch one.',
                                                   rationale='New reason.')
        resp = self.client.post(
            reverse('tickets:sms_plan_regenerate_step', kwargs={'pk': plan.id, 'step': 0}),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['body'], 'Totally fresh copy for touch one.')
        self.assertEqual(data['rationale'], 'New reason.')
        self.assertGreaterEqual(data['segments'], 1)

        plan.refresh_from_db()
        self.assertEqual(plan.steps[0]['body'], 'Totally fresh copy for touch one.')
        self.assertNotEqual(plan.steps[0]['body'], old_body)
        self.assertEqual(plan.steps[0]['rationale'], 'New reason.')
        # Schedule + audience are untouched by a message regenerate.
        self.assertEqual(plan.steps[0]['send_at'], old_send_at)
        self.assertEqual(plan.steps[0]['audience_criteria'], old_audience)

        # A second billable usage row is recorded, tagged as a regenerate.
        regen_usage = AITokenUsage.objects.filter(
            organization=self.org, feature=AITokenUsage.FEATURE_SMS_PLAN,
            metadata__regenerate=True,
        )
        self.assertEqual(regen_usage.count(), 1)

    @patch('langchain_openai.ChatOpenAI')
    def test_regenerate_step_refuses_launched_step(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        # Mark step 0 as already launched.
        steps = plan.steps
        steps[0]['launched_campaign_id'] = str(uuid.uuid4())
        plan.steps = steps
        plan.save(update_fields=['steps'])
        frozen_body = plan.steps[0]['body']

        resp = self.client.post(
            reverse('tickets:sms_plan_regenerate_step', kwargs={'pk': plan.id, 'step': 0}),
        )
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.json()['ok'])
        plan.refresh_from_db()
        self.assertEqual(plan.steps[0]['body'], frozen_body)

    @patch('langchain_openai.ChatOpenAI')
    def test_regenerate_plan_replaces_steps_keeps_name(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        # User renamed the plan; regenerate must preserve it.
        plan.name = 'My custom plan name'
        plan.save(update_fields=['name'])
        # Edit a message so we can prove regenerate discards edits.
        steps = plan.steps
        steps[0]['body'] = 'HAND EDITED'
        plan.steps = steps
        plan.save(update_fields=['steps'])

        mock_openai.return_value = _fake_structured_llm()
        resp = self.client.post(reverse('tickets:sms_plan_regenerate', kwargs={'pk': plan.id}))
        self.assertRedirects(resp, reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        plan.refresh_from_db()
        # Name kept; steps replaced with a fresh sequence (edit gone).
        self.assertEqual(plan.name, 'My custom plan name')
        self.assertEqual(len(plan.steps), 3)
        self.assertNotIn('HAND EDITED', [s['body'] for s in plan.steps])
        self.assertEqual(plan.status, SMSCampaignPlan.Status.DRAFT)

    @patch('langchain_openai.ChatOpenAI')
    def test_regenerate_plan_blocked_when_step_launched(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        steps = plan.steps
        steps[1]['launched_campaign_id'] = str(uuid.uuid4())
        plan.steps = steps
        plan.save(update_fields=['steps'])
        before = [s['body'] for s in plan.steps]

        resp = self.client.post(reverse('tickets:sms_plan_regenerate', kwargs={'pk': plan.id}))
        self.assertRedirects(resp, reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        plan.refresh_from_db()
        # Nothing regenerated — steps unchanged.
        self.assertEqual([s['body'] for s in plan.steps], before)

    @patch('langchain_openai.ChatOpenAI')
    def test_update_schedule_rejects_bad_datetime(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
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
        resp = self._gen({'rfm_segment': ['VIP']})
        # Generate redirects to the unsaved preview; _gen's Save-as-draft then persists the
        # plan (which consumes the session preview, so don't re-fetch that page here).
        self.assertRedirects(resp, reverse('tickets:sms_plan_preview'),
                             fetch_redirect_response=False)
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        self.assertIsNone(plan.event_id)
        self.assertEqual(plan.filter_criteria, {'rfm_segment': ['VIP']})

    def test_empty_audience_is_rejected(self):
        resp = self._gen({})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(SMSCampaignPlan.objects.count(), 0)

    @patch('langchain_openai.ChatOpenAI')
    def test_gated_by_flag(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.org.ai_sms_strategist_enabled = False
        self.org.save(update_fields=['ai_sms_strategist_enabled'])
        get = self.client.get(reverse('tickets:sms_plan_create'))
        self.assertEqual(get.status_code, 404)
        post = self._gen({'event': str(self.event.id)})
        self.assertEqual(post.status_code, 404)
        self.assertEqual(SMSCampaignPlan.objects.count(), 0)

    @patch('langchain_openai.ChatOpenAI')
    def test_plan_detail_org_scoped(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
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
    def test_launch_step_prefills_composer_without_marking_launched(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
        plan = SMSCampaignPlan.objects.get(organization=self.org)

        resp = self.client.post(
            reverse('tickets:sms_plan_launch_step', kwargs={'pk': plan.id, 'step': 0}),
        )
        # Redirects into the composer, pinned to the event.
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('tickets:sms_campaign_create'), resp.url)
        self.assertIn(f'event={self.event.id}', resp.url)
        # Session prefill carries the written body + audience + plan linkage.
        prefill = self.client.session['sms_compose_prefill']
        self.assertEqual(prefill['body'], plan.steps[0]['body'])
        self.assertEqual(prefill['event_id'], str(self.event.id))
        self.assertEqual(prefill['plan_id'], str(plan.id))
        self.assertEqual(prefill['step'], 0)
        # Opening the editor is NOT a send: the step must not be marked launched.
        plan.refresh_from_db()
        self.assertNotIn('launched_at', plan.steps[0])
        self.assertIsNone(plan.steps[0].get('launched_campaign_id'))

    @patch('langchain_openai.ChatOpenAI')
    def test_launched_body_prefills_composer_form(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'rfm_segment': ['VIP']})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        self.client.post(reverse('tickets:sms_plan_launch_step', kwargs={'pk': plan.id, 'step': 0}))
        # The composer GET renders the prefilled body into the form.
        resp = self.client.get(reverse('tickets:sms_campaign_create'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, plan.steps[0]['body'])

    @patch('langchain_openai.ChatOpenAI')
    def test_update_step_persists_edit_and_returns_segments(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self._gen({'event': str(self.event.id)})
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
        self._gen({'event': str(self.event.id)})
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
        self._gen({'event': str(self.event.id)})
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
        # Opening the editor doesn't mark launched, even with a body override.
        self.assertNotIn('launched_at', plan.steps[0])

    # --- Inline confirm & send (from the plan page, no composer) ---------------

    def _make_event_plan(self):
        self._gen({'event': str(self.event.id)})
        return SMSCampaignPlan.objects.get(organization=self.org)

    @patch('langchain_openai.ChatOpenAI')
    def test_plan_preview_step_returns_count_cost_and_key(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()

        resp = self.client.post(
            reverse('tickets:sms_plan_preview_step', kwargs={'pk': plan.id, 'step': 0}),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['recipient_count'], 1)      # the one opted-in customer
        self.assertGreaterEqual(data['cost_tokens'], 1)
        self.assertFalse(data['insufficient'])
        self.assertTrue(data['scheduled'])                # step 0 is ~14 days out
        self.assertIn('idempotency_key', data)
        self.assertFalse(data['already_launched'])

    @patch('langchain_openai.ChatOpenAI')
    def test_plan_confirm_step_creates_campaign_charges_and_marks_launched(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        self.org.refresh_from_db()
        start_balance = self.org.sms_credit_balance_cents

        resp = self.client.post(
            reverse('tickets:sms_plan_confirm_step', kwargs={'pk': plan.id, 'step': 0}),
            {'idempotency_key': 'abc123'},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        c = SMSCampaign.objects.get(organization=self.org)
        self.assertEqual(str(c.id), data['campaign_id'])
        self.assertEqual(c.status, SMSCampaign.Status.SCHEDULED)
        self.assertEqual(c.audience_size, 1)
        # all_subscribers on an event plan keeps the campaign linked to the event.
        self.assertEqual(c.event_id, self.event.id)
        self.assertEqual(SMSMessageRecipient.objects.filter(campaign=c).count(), 1)
        # The wallet was debited.
        self.org.refresh_from_db()
        self.assertLess(self.org.sms_credit_balance_cents, start_balance)
        # The step is stamped launched + linked to the new campaign.
        plan.refresh_from_db()
        self.assertEqual(plan.steps[0]['launched_campaign_id'], str(c.id))
        self.assertIn('launched_at', plan.steps[0])

    @patch('langchain_openai.ChatOpenAI')
    def test_market_step_links_campaign_to_plan_event(self, mock_openai):
        # Regression: when the event's venue matches a Market, plan steps get a
        # {'market_ids': [...]} audience (not all_subscribers). The launched campaign must
        # still link to the plan's event so it shows up on the SMS list + event marketing tab.
        mock_openai.return_value = _fake_structured_llm()
        market = Market.objects.create(
            organization=self.org, name='Austin', geography_level=MARKET_GEOGRAPHY_CITY,
            geography_value='Austin',
        )
        plan = self._make_event_plan()
        # Sanity: the plan really did resolve to a market audience (the buggy shape).
        self.assertEqual(plan.steps[0]['audience_criteria'], {'market_ids': [str(market.id)]})

        event = _plan_step_event(self.org, plan, plan.steps[0]['audience_criteria'])
        self.assertIsNotNone(event)
        self.assertEqual(event.id, self.event.id)

    @patch('langchain_openai.ChatOpenAI')
    def test_launched_step_shows_scheduled_not_launched(self, mock_openai):
        # A step confirmed with a future send time is scheduled — the plan must reflect
        # "Scheduled", not a generic "Launched".
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()   # step 0 send_at is ~14 days out (future)
        self.client.post(
            reverse('tickets:sms_plan_confirm_step', kwargs={'pk': plan.id, 'step': 0}),
            {'idempotency_key': 'k'},
        )
        campaign = SMSCampaign.objects.get(organization=self.org)
        self.assertEqual(campaign.status, SMSCampaign.Status.SCHEDULED)

        resp = self.client.get(reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertContains(resp, 'Scheduled')
        # The confirmed step is not mislabeled "Launched".
        self.assertNotContains(resp, '> Launched</span>')

    @patch('langchain_openai.ChatOpenAI')
    def test_plan_confirm_step_is_idempotent(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        url = reverse('tickets:sms_plan_confirm_step', kwargs={'pk': plan.id, 'step': 0})
        self.client.post(url, {'idempotency_key': 'k1'})
        self.org.refresh_from_db()
        after_first = self.org.sms_credit_balance_cents
        # A second confirm (step already launched) must not create or charge again.
        resp = self.client.post(url, {'idempotency_key': 'k2'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get('already_launched'))
        self.assertEqual(SMSCampaign.objects.filter(organization=self.org).count(), 1)
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, after_first)

    @patch('langchain_openai.ChatOpenAI')
    def test_plan_confirm_step_insufficient_credits_blocks(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        self.org.sms_credit_balance_cents = 0
        self.org.save(update_fields=['sms_credit_balance_cents'])

        resp = self.client.post(
            reverse('tickets:sms_plan_confirm_step', kwargs={'pk': plan.id, 'step': 0}),
            {'idempotency_key': 'k'},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()['ok'])
        self.assertEqual(SMSCampaign.objects.filter(organization=self.org).count(), 0)
        plan.refresh_from_db()
        self.assertIsNone(plan.steps[0].get('launched_campaign_id'))

    @patch('langchain_openai.ChatOpenAI')
    def test_plan_confirm_step_past_time_sends_now(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        # Force the step's suggested time into the past.
        steps = plan.steps
        steps[0]['send_at'] = (timezone.now() - timedelta(days=1)).isoformat()
        plan.steps = steps
        plan.save(update_fields=['steps'])

        resp = self.client.post(
            reverse('tickets:sms_plan_confirm_step', kwargs={'pk': plan.id, 'step': 0}),
            {'idempotency_key': 'k'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()['scheduled'])   # past -> send now, not scheduled
        c = SMSCampaign.objects.get(organization=self.org)
        self.assertLessEqual(c.scheduled_at, timezone.now() + timedelta(seconds=5))

    @patch('langchain_openai.ChatOpenAI')
    def test_plan_preview_confirm_scoped_and_gated(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        preview = reverse('tickets:sms_plan_preview_step', kwargs={'pk': plan.id, 'step': 0})
        # GET is not allowed on these POST-only endpoints.
        self.assertEqual(self.client.get(preview).status_code, 405)
        # Out-of-range step -> 404.
        bad = reverse('tickets:sms_plan_preview_step', kwargs={'pk': plan.id, 'step': 9})
        self.assertEqual(self.client.post(bad).status_code, 404)
        # Feature gate off -> 404.
        self.org.ai_sms_strategist_enabled = False
        self.org.save(update_fields=['ai_sms_strategist_enabled'])
        self.assertEqual(self.client.post(preview).status_code, 404)

    @patch('langchain_openai.ChatOpenAI')
    def test_full_editor_send_marks_origin_plan_step(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        original_body = plan.steps[0]['body']
        # Open in full editor — carries plan_id/step in the session prefill.
        self.client.post(reverse('tickets:sms_plan_launch_step', kwargs={'pk': plan.id, 'step': 0}))
        # Send from the composer with a body edited after the handoff.
        resp = self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': 'From editor', 'body': 'Hello from the editor!',
            'send_mode': 'now', 'event': str(self.event.id), 'audience_scope': 'all',
            'confirm': '1', 'prefill_plan_id': str(plan.id), 'prefill_step': '0',
        })
        self.assertEqual(resp.status_code, 302)
        c = SMSCampaign.objects.get(organization=self.org)
        plan.refresh_from_db()
        self.assertEqual(plan.steps[0]['launched_campaign_id'], str(c.id))
        # The step's body is synced to what actually sent (the composer edit), not the
        # stale AI-written body it launched with — so the plan matches the campaign.
        self.assertEqual(plan.steps[0]['body'], c.body)
        self.assertEqual(plan.steps[0]['body'], 'Hello from the editor!')
        self.assertNotEqual(plan.steps[0]['body'], original_body)

    @patch('langchain_openai.ChatOpenAI')
    def test_mark_launched_syncs_body_and_recomputes_segments(self, mock_openai):
        # A launched step adopts the campaign's final body (e.g. the minted per-campaign
        # tracking link) and recomputes its segment/encoding meter to match.
        from tickets.sms_views import _mark_plan_step_launched
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        sent_body = 'TEMPO IS BACK AT MELROSE ON FRIDAY! Tix: https://cueup.co/t/3P2aZxkU8fve/'
        campaign = SMSCampaign.objects.create(
            organization=self.org, name='sent', body=sent_body,
            status=SMSCampaign.Status.SENT)
        _mark_plan_step_launched(self.org, plan.id, 0, campaign.id)
        plan.refresh_from_db()
        step = plan.steps[0]
        self.assertEqual(step['body'], sent_body)
        self.assertEqual(step['launched_campaign_id'], str(campaign.id))
        self.assertIn('segments', step)
        self.assertIn('encoding', step)

    @patch('langchain_openai.ChatOpenAI')
    def test_open_in_full_editor_prefills_market(self, mock_openai):
        # A market-targeted step stores market_ids (plural); the composer's single market
        # field must still pre-select that market when opened in the full editor — it must
        # not silently reset to "All markets".
        market = Market.objects.create(
            organization=self.org, name='Austin', geography_level=MARKET_GEOGRAPHY_CITY,
            geography_value='Austin',
        )
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        self.assertEqual(plan.steps[0]['audience_criteria'], {'market_ids': [str(market.id)]})

        # Open in full editor → prefill carries the market; composer GET pre-selects it.
        self.client.post(reverse('tickets:sms_plan_launch_step', kwargs={'pk': plan.id, 'step': 0}))
        resp = self.client.get(reverse('tickets:sms_campaign_create'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['form'].initial.get('market_id'), str(market.id))

    @patch('langchain_openai.ChatOpenAI')
    def test_full_editor_back_link_returns_to_plan(self, mock_openai):
        # Opening a step in the full editor must offer a back link to the plan (so the
        # organizer doesn't lose their place); a plain composer visit links to the SMS list.
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        self.client.post(reverse('tickets:sms_plan_launch_step', kwargs={'pk': plan.id, 'step': 0}))
        resp = self.client.get(reverse('tickets:sms_campaign_create'))
        self.assertContains(resp, reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertContains(resp, 'Back to plan')

        # A direct composer visit (no plan prefill) keeps the SMS Campaigns back link.
        plain = self.client.get(reverse('tickets:sms_campaign_create'))
        self.assertNotContains(plain, 'Back to plan')
        self.assertContains(plain, reverse('tickets:sms_campaign_list'))

    # --- Draft / In-progress status (auto-derived) ----------------------------

    @patch('langchain_openai.ChatOpenAI')
    def test_generated_plan_defaults_to_draft(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        self.assertEqual(plan.status, SMSCampaignPlan.Status.DRAFT)

    @patch('langchain_openai.ChatOpenAI')
    def test_plan_status_progresses_in_progress_then_scheduled(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        self.assertEqual(len(plan.steps), 3)
        # Nothing launched yet → Draft.
        self.assertEqual(plan.status, SMSCampaignPlan.Status.DRAFT)
        # Confirm the first two (both scheduled for the future). A draft step remains, so the
        # plan is now partly underway → In progress.
        for i in (0, 1):
            self.client.post(
                reverse('tickets:sms_plan_confirm_step', kwargs={'pk': plan.id, 'step': i}),
                {'idempotency_key': f'k{i}'},
            )
        plan.refresh_from_db()
        self.assertEqual(plan.status, SMSCampaignPlan.Status.IN_PROGRESS)
        # Confirm the last — every step is scheduled and none is still draft → Scheduled.
        self.client.post(
            reverse('tickets:sms_plan_confirm_step', kwargs={'pk': plan.id, 'step': 2}),
            {'idempotency_key': 'k2'},
        )
        plan.refresh_from_db()
        self.assertEqual(plan.status, SMSCampaignPlan.Status.SCHEDULED)

    @patch('langchain_openai.ChatOpenAI')
    def test_removing_last_unscheduled_step_advances_status(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        for i in (0, 1):
            self.client.post(
                reverse('tickets:sms_plan_confirm_step', kwargs={'pk': plan.id, 'step': i}),
                {'idempotency_key': f'k{i}'},
            )
        plan.refresh_from_db()
        self.assertEqual(plan.status, SMSCampaignPlan.Status.IN_PROGRESS)
        # Remove the only remaining draft step → every remaining step is scheduled → Scheduled.
        self.client.post(reverse('tickets:sms_plan_remove_step', kwargs={'pk': plan.id, 'step': 2}))
        plan.refresh_from_db()
        self.assertEqual(plan.status, SMSCampaignPlan.Status.SCHEDULED)

    def _plan_with_steps(self, name, statuses, stored_status):
        """Build a plan whose steps map to given campaign statuses.

        ``None`` → an unlaunched (draft) step; any ``SMSCampaign.Status`` value → a launched
        step linked to a campaign in that state. ``stored_status`` seeds ``plan.status`` so it
        matches the steps (the list/detail views self-heal, so a mismatch would drift).
        """
        steps = []
        for i, st in enumerate(statuses):
            step = {'order': i, 'body': 'hi', 'segments': 1}
            if st is not None:
                campaign = SMSCampaign.objects.create(
                    organization=self.org, name=f'{name}{i}', body='hi', status=st)
                step['launched_campaign_id'] = str(campaign.id)
            steps.append(step)
        return SMSCampaignPlan.objects.create(
            organization=self.org, name=name, steps=steps, status=stored_status)

    def test_plan_list_filters_by_status(self):
        S = SMSCampaign.Status
        P = SMSCampaignPlan.Status
        self._plan_with_steps('Draft plan', [None], P.DRAFT)
        self._plan_with_steps('Running plan', [S.SCHEDULED, None], P.IN_PROGRESS)
        self._plan_with_steps('Queued plan', [S.SCHEDULED], P.SCHEDULED)
        self._plan_with_steps('Done plan', [S.SENT], P.SENT)
        everyone = {'Draft plan', 'Running plan', 'Queued plan', 'Done plan'}

        def names(status=None):
            params = {'status': status} if status else {}
            resp = self.client.get(reverse('tickets:sms_plan_list'), params)
            return {p.name for p in resp.context['page_obj'].object_list} & everyone

        self.assertEqual(names('draft'), {'Draft plan'})
        self.assertEqual(names('in_progress'), {'Running plan'})
        self.assertEqual(names('scheduled'), {'Queued plan'})
        self.assertEqual(names('sent'), {'Done plan'})
        self.assertEqual(names(), everyone)

    @patch('langchain_openai.ChatOpenAI')
    def test_plan_detail_badge_and_no_manual_toggle(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        resp = self.client.get(reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertContains(resp, '>Draft<')
        self.assertNotContains(resp, 'Mark as ready')      # manual toggle removed

    def test_plan_detail_badge_reflects_step_mix(self):
        S = SMSCampaign.Status
        P = SMSCampaignPlan.Status

        def badge(plan):
            resp = self.client.get(reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
            return resp.content.decode()

        # One sent + one still draft → In progress, with the step breakdown rendered.
        partial = self._plan_with_steps('Partial', [S.SENT, None], P.IN_PROGRESS)
        html = badge(partial)
        self.assertIn('>In progress<', html)
        self.assertIn('1 sent', html)
        self.assertIn('1 draft', html)
        # Every step scheduled → Scheduled.
        self.assertIn('>Scheduled<', badge(self._plan_with_steps('Queued', [S.SCHEDULED], P.SCHEDULED)))
        # Every step sent → Sent.
        self.assertIn('>Sent<', badge(self._plan_with_steps('Done', [S.SENT], P.SENT)))

    def test_plan_detail_self_heals_status_on_async_send(self):
        # A Scheduled plan whose campaign sends out-of-band (Celery, not a plan mutation) must
        # render Sent and persist the corrected status for the filter tabs.
        S = SMSCampaign.Status
        plan = self._plan_with_steps('Queued', [S.SCHEDULED], SMSCampaignPlan.Status.SCHEDULED)
        SMSCampaign.objects.filter(organization=self.org).update(status=S.SENT)
        resp = self.client.get(reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertIn('>Sent<', resp.content.decode())
        plan.refresh_from_db()
        self.assertEqual(plan.status, SMSCampaignPlan.Status.SENT)

    def test_plan_progress_buckets(self):
        S = SMSCampaign.Status
        P = SMSCampaignPlan.Status

        def progress(statuses):
            plan = self._plan_with_steps('P', statuses, P.DRAFT)
            return _plan_progress(self.org, plan.steps)

        self.assertEqual(_plan_progress(self.org, []),
                         {'total': 0, 'draft': 0, 'scheduled': 0, 'sent': 0, 'status': P.DRAFT})
        self.assertEqual(progress([None, None])['status'], P.DRAFT)
        # Case 1: some scheduled, a draft remains.
        p1 = progress([S.SCHEDULED, None])
        self.assertEqual((p1['scheduled'], p1['draft'], p1['status']), (1, 1, P.IN_PROGRESS))
        # Case 2: all launched, at least one still queued.
        self.assertEqual(progress([S.SCHEDULED, S.SENT])['status'], P.SCHEDULED)
        self.assertEqual(progress([S.SENT, S.SENT])['status'], P.SENT)
        # Canceled/failed launched steps fold into the draft (needs-action) bucket.
        pc = progress([S.CANCELED, S.SCHEDULED])
        self.assertEqual((pc['draft'], pc['scheduled'], pc['status']), (1, 1, P.IN_PROGRESS))

    def test_manual_status_route_removed(self):
        from django.urls import NoReverseMatch
        with self.assertRaises(NoReverseMatch):
            reverse('tickets:sms_plan_set_status',
                    kwargs={'pk': '00000000-0000-0000-0000-000000000000'})

    # --- Discover / discard plans ---------------------------------------------

    def test_sms_page_links_to_plan_list(self):
        # The SMS marketing page must surface a "Plans" link so past plans are reachable.
        resp = self.client.get(reverse('tickets:sms_campaign_list'))
        self.assertContains(resp, reverse('tickets:sms_plan_list'))
        self.assertContains(resp, '>Plans</a>')

    @patch('langchain_openai.ChatOpenAI')
    def test_plan_list_and_detail_expose_delete(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        delete_url = reverse('tickets:sms_plan_delete', kwargs={'pk': plan.id})
        lst = self.client.get(reverse('tickets:sms_plan_list'))
        self.assertContains(lst, delete_url)                       # per-row delete form
        detail = self.client.get(reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertContains(detail, delete_url)                    # header Delete button form

    @patch('langchain_openai.ChatOpenAI')
    def test_plan_delete_removes_plan_and_redirects(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        resp = self.client.post(reverse('tickets:sms_plan_delete', kwargs={'pk': plan.id}))
        self.assertRedirects(resp, reverse('tickets:sms_plan_list'))
        self.assertFalse(SMSCampaignPlan.objects.filter(id=plan.id).exists())

    @patch('langchain_openai.ChatOpenAI')
    def test_plan_delete_leaves_launched_campaigns_intact(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        self.client.post(
            reverse('tickets:sms_plan_confirm_step', kwargs={'pk': plan.id, 'step': 0}),
            {'idempotency_key': 'k'},
        )
        self.assertEqual(SMSCampaign.objects.filter(organization=self.org).count(), 1)
        self.client.post(reverse('tickets:sms_plan_delete', kwargs={'pk': plan.id}))
        # Discarding the plan must not delete a campaign already launched from it.
        self.assertEqual(SMSCampaign.objects.filter(organization=self.org).count(), 1)

    @patch('langchain_openai.ChatOpenAI')
    def test_plan_delete_blocked_once_a_message_is_sent(self, mock_openai):
        # Once any message has sent, the whole plan can no longer be deleted.
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        campaign = SMSCampaign.objects.create(
            organization=self.org, name='sent', body=plan.steps[0]['body'],
            status=SMSCampaign.Status.SENT)
        plan.steps[0]['launched_campaign_id'] = str(campaign.id)
        plan.steps[0]['launched_at'] = timezone.now().isoformat()
        plan.save(update_fields=['steps'])

        resp = self.client.post(reverse('tickets:sms_plan_delete', kwargs={'pk': plan.id}))
        self.assertRedirects(resp, reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertTrue(SMSCampaignPlan.objects.filter(id=plan.id).exists())

        # Neither the detail header nor the list row offers a delete control anymore.
        delete_url = reverse('tickets:sms_plan_delete', kwargs={'pk': plan.id})
        detail = self.client.get(reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertNotContains(detail, delete_url)
        lst = self.client.get(reverse('tickets:sms_plan_list'))
        self.assertNotContains(lst, delete_url)

    @patch('langchain_openai.ChatOpenAI')
    def test_plan_delete_gated_scoped_and_post_only(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        url = reverse('tickets:sms_plan_delete', kwargs={'pk': plan.id})
        self.assertEqual(self.client.get(url).status_code, 405)     # POST-only
        # Other org cannot delete this plan.
        other = Organization.objects.create(name='Other', slug='other-del',
                                            sms_marketing_enabled=True, ai_sms_strategist_enabled=True)
        ouser = User.objects.create_user('od', 'od@test.com', 'pw')
        UserProfile.objects.create(user=ouser, organization=other, org_role=UserProfile.OrgRole.OWNER)
        oclient = Client(); oclient.login(username='od@test.com', password='pw'); oclient.get(reverse('tickets:home'))
        self.assertEqual(oclient.post(url).status_code, 404)
        self.assertTrue(SMSCampaignPlan.objects.filter(id=plan.id).exists())
        # Feature gate off -> 404.
        self.org.ai_sms_strategist_enabled = False
        self.org.save(update_fields=['ai_sms_strategist_enabled'])
        self.assertEqual(self.client.post(url).status_code, 404)
        self.assertTrue(SMSCampaignPlan.objects.filter(id=plan.id).exists())

    @patch('langchain_openai.ChatOpenAI')
    def test_plan_rename_updates_name(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        resp = self.client.post(
            reverse('tickets:sms_plan_rename', kwargs={'pk': plan.id}),
            {'name': '  VIP early-bird push  '},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'ok': True, 'name': 'VIP early-bird push'})
        plan.refresh_from_db()
        self.assertEqual(plan.name, 'VIP early-bird push')          # trimmed + persisted
        # And it shows on the detail page + the plans list.
        detail = self.client.get(reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertContains(detail, 'VIP early-bird push')
        self.assertContains(self.client.get(reverse('tickets:sms_plan_list')), 'VIP early-bird push')

    @patch('langchain_openai.ChatOpenAI')
    def test_plan_rename_rejects_empty(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        before = plan.name
        resp = self.client.post(
            reverse('tickets:sms_plan_rename', kwargs={'pk': plan.id}), {'name': '   '})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()['ok'])
        plan.refresh_from_db()
        self.assertEqual(plan.name, before)                          # unchanged

    @patch('langchain_openai.ChatOpenAI')
    def test_plan_rename_gated_scoped_and_post_only(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        url = reverse('tickets:sms_plan_rename', kwargs={'pk': plan.id})
        self.assertEqual(self.client.get(url).status_code, 405)       # POST-only
        # Other org cannot rename this plan.
        other = Organization.objects.create(name='Other', slug='other-ren',
                                            sms_marketing_enabled=True, ai_sms_strategist_enabled=True)
        ouser = User.objects.create_user('or', 'or@test.com', 'pw')
        UserProfile.objects.create(user=ouser, organization=other, org_role=UserProfile.OrgRole.OWNER)
        oclient = Client(); oclient.login(username='or@test.com', password='pw'); oclient.get(reverse('tickets:home'))
        self.assertEqual(oclient.post(url, {'name': 'Hijack'}).status_code, 404)
        # Feature gate off -> 404.
        self.org.ai_sms_strategist_enabled = False
        self.org.save(update_fields=['ai_sms_strategist_enabled'])
        self.assertEqual(self.client.post(url, {'name': 'Nope'}).status_code, 404)

    # --- Unsaved preview → Save as draft / Discard (generate no longer auto-saves) ----

    @patch('langchain_openai.ChatOpenAI')
    def test_generate_stashes_preview_without_persisting(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        resp = self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        self.assertRedirects(resp, reverse('tickets:sms_plan_preview'))
        # Nothing saved yet, but the generated plan is held in the session.
        self.assertEqual(SMSCampaignPlan.objects.count(), 0)
        preview = self.client.session['sms_plan_preview']
        self.assertEqual(preview['event_id'], str(self.event.id))
        self.assertEqual(len(preview['steps']), 3)

    @patch('langchain_openai.ChatOpenAI')
    def test_preview_page_renders_unsaved_and_save_button(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        resp = self.client.get(reverse('tickets:sms_plan_preview'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Unsaved')
        self.assertContains(resp, 'Save Plan')
        # Messages, send times, and audiences are all editable in place before the first save.
        self.assertContains(resp, 'class="plan-step-message"')
        self.assertContains(resp, 'plan-timing-input')          # inline send-time editor
        self.assertContains(resp, 'id="audienceModal"')         # audience editor
        # The AI's first message body renders in the editable preview.
        self.assertContains(resp, 'Tickets are live for the show')

    @patch('langchain_openai.ChatOpenAI')
    def test_save_applies_inline_message_edits(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        edited = 'Organizer-edited copy before saving.'
        resp = self.client.post(reverse('tickets:sms_plan_save'), {'step_body_0': edited})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        self.assertRedirects(resp, reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        # The edited body is persisted (with a recomputed segment count); other steps intact.
        self.assertEqual(plan.steps[0]['body'], edited)
        self.assertGreaterEqual(plan.steps[0]['segments'], 1)
        self.assertNotEqual(plan.steps[1]['body'], edited)

    @patch('langchain_openai.ChatOpenAI')
    def test_save_applies_inline_timing_edit(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        self.client.post(reverse('tickets:sms_plan_save'), {'step_send_0': '2026-09-01T18:30'})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        from datetime import datetime
        self.assertEqual(datetime.fromisoformat(plan.steps[0]['send_at']).strftime('%Y-%m-%d %H:%M'),
                         '2026-09-01 18:30')
        self.assertEqual(plan.steps[0]['send_time'], '18:30')
        self.assertTrue(plan.steps[0]['timing_label'])   # recomputed, tz-aware

    @patch('langchain_openai.ChatOpenAI')
    def test_save_applies_inline_audience_edit(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        import json
        self.client.post(reverse('tickets:sms_plan_save'),
                         {'step_audience_0': json.dumps({'rfm_segment': ['VIP']})})
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        self.assertEqual(plan.steps[0]['audience_criteria'], {'rfm_segment': ['VIP']})
        self.assertIn('VIP', plan.steps[0]['audience_label'])
        # Untouched steps keep their generated (all-subscribers) audience.
        self.assertEqual(plan.steps[1]['audience_criteria'], {'all_subscribers': True})

    @patch('langchain_openai.ChatOpenAI')
    def test_resolve_audience_endpoint(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        url = reverse('tickets:sms_plan_resolve_audience')
        # Custom segment selection.
        data = self.client.post(url, {'audience_mode': 'custom', 'rfm_segment': ['VIP']}).json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['criteria'], {'rfm_segment': ['VIP']})
        self.assertIn('VIP', data['audience_label'])
        # Event / all-subscribers modes resolve against the posted event.
        ev = self.client.post(url, {'audience_mode': 'event', 'event_id': str(self.event.id)}).json()
        self.assertEqual(ev['criteria'], {'event_id': str(self.event.id)})
        allsub = self.client.post(url, {'audience_mode': 'all', 'event_id': str(self.event.id)}).json()
        self.assertEqual(allsub['criteria'], {'all_subscribers': True})
        # Empty custom selection is rejected; gate off -> 404.
        self.assertEqual(self.client.post(url, {'audience_mode': 'custom'}).status_code, 400)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.org.ai_sms_strategist_enabled = False
        self.org.save(update_fields=['ai_sms_strategist_enabled'])
        self.assertEqual(self.client.post(url, {'audience_mode': 'custom', 'rfm_segment': ['VIP']}).status_code, 404)

    def test_preview_without_session_redirects_to_generate(self):
        resp = self.client.get(reverse('tickets:sms_plan_preview'))
        self.assertRedirects(resp, reverse('tickets:sms_plan_create'))

    @patch('langchain_openai.ChatOpenAI')
    def test_save_persists_draft_and_clears_session(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        resp = self.client.post(reverse('tickets:sms_plan_save'))
        plan = SMSCampaignPlan.objects.get(organization=self.org)
        self.assertRedirects(resp, reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertEqual(plan.status, SMSCampaignPlan.Status.DRAFT)
        self.assertEqual(plan.event_id, self.event.id)
        self.assertNotIn('sms_plan_preview', self.client.session)

    def test_save_without_session_creates_nothing(self):
        resp = self.client.post(reverse('tickets:sms_plan_save'))
        self.assertRedirects(resp, reverse('tickets:sms_plan_create'))
        self.assertEqual(SMSCampaignPlan.objects.count(), 0)

    @patch('langchain_openai.ChatOpenAI')
    def test_discard_drops_preview_without_persisting(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        resp = self.client.post(reverse('tickets:sms_plan_discard'))
        self.assertRedirects(resp, reverse('tickets:sms_plan_list'))
        self.assertEqual(SMSCampaignPlan.objects.count(), 0)
        self.assertNotIn('sms_plan_preview', self.client.session)

    @patch('langchain_openai.ChatOpenAI')
    def test_discard_beacon_returns_json(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        resp = self.client.post(reverse('tickets:sms_plan_discard'), {'beacon': '1'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['ok'])
        self.assertNotIn('sms_plan_preview', self.client.session)

    @patch('langchain_openai.ChatOpenAI')
    def test_regenerate_from_preview_reuses_stashed_event(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        # "Regenerate" posts from_preview WITHOUT an event field — it must reuse the event
        # stashed in the preview, re-stash a fresh preview, and still persist nothing.
        resp = self.client.post(reverse('tickets:sms_plan_create'), {'from_preview': '1'})
        self.assertRedirects(resp, reverse('tickets:sms_plan_preview'))
        self.assertEqual(SMSCampaignPlan.objects.count(), 0)
        self.assertEqual(self.client.session['sms_plan_preview']['event_id'], str(self.event.id))

    @patch('langchain_openai.ChatOpenAI')
    def test_preview_save_discard_gated_by_flag(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.client.post(reverse('tickets:sms_plan_create'), {'event': str(self.event.id)})
        self.org.ai_sms_strategist_enabled = False
        self.org.save(update_fields=['ai_sms_strategist_enabled'])
        self.assertEqual(self.client.get(reverse('tickets:sms_plan_preview')).status_code, 404)
        self.assertEqual(self.client.post(reverse('tickets:sms_plan_save')).status_code, 404)
        self.assertEqual(self.client.post(reverse('tickets:sms_plan_discard')).status_code, 404)
        self.assertEqual(SMSCampaignPlan.objects.count(), 0)

    # --- Enabled/Disabled toggle + paused-send hold + overdue resolution -------

    def _confirm_step(self, plan, step=0, key='k'):
        self.client.post(
            reverse('tickets:sms_plan_confirm_step', kwargs={'pk': plan.id, 'step': step}),
            {'idempotency_key': key},
        )
        return SMSCampaign.objects.filter(organization=self.org).latest('created_at')

    @patch('langchain_openai.ChatOpenAI')
    def test_new_plan_enabled_by_default(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        self.assertTrue(self._make_event_plan().enabled)

    @patch('langchain_openai.ChatOpenAI')
    def test_toggle_enabled_endpoint(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        url = reverse('tickets:sms_plan_toggle_enabled', kwargs={'pk': plan.id})
        # Default flip on -> off, then explicit enable.
        self.assertFalse(self.client.post(url).json()['enabled'])
        plan.refresh_from_db(); self.assertFalse(plan.enabled)
        self.assertTrue(self.client.post(url, {'enabled': '1'}).json()['enabled'])
        plan.refresh_from_db(); self.assertTrue(plan.enabled)
        # POST-only + feature-gated.
        self.assertEqual(self.client.get(url).status_code, 405)
        self.org.ai_sms_strategist_enabled = False
        self.org.save(update_fields=['ai_sms_strategist_enabled'])
        self.assertEqual(self.client.post(url).status_code, 404)

    @patch('langchain_openai.ChatOpenAI')
    def test_confirm_links_campaign_to_plan(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        self.assertEqual(self._confirm_step(plan).plan_id, plan.id)

    @patch('langchain_openai.ChatOpenAI')
    def test_confirm_blocked_when_disabled(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        plan.enabled = False
        plan.save(update_fields=['enabled'])
        resp = self.client.post(
            reverse('tickets:sms_plan_confirm_step', kwargs={'pk': plan.id, 'step': 0}),
            {'idempotency_key': 'k'},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.json()['ok'])
        self.assertEqual(SMSCampaign.objects.filter(organization=self.org).count(), 0)

    @patch('tickets.sms.send_sms', side_effect=lambda to, body, status_callback=None: (True, 'SM' + to[-4:], None))
    @patch('langchain_openai.ChatOpenAI')
    def test_disabled_plan_holds_send_but_force_overrides(self, mock_openai, mock_send):
        from .tasks import send_sms_campaign_task
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        c = self._confirm_step(plan)
        self.assertEqual(c.status, SMSCampaign.Status.SCHEDULED)
        # Disabled → the send task holds it (stays SCHEDULED, nothing sent).
        plan.enabled = False
        plan.save(update_fields=['enabled'])
        send_sms_campaign_task.delay(str(c.id))
        c.refresh_from_db()
        self.assertEqual(c.status, SMSCampaign.Status.SCHEDULED)
        self.assertFalse(mock_send.called)
        # force=True sends despite the disabled plan.
        send_sms_campaign_task.delay(str(c.id), force=True)
        c.refresh_from_db()
        self.assertEqual(c.status, SMSCampaign.Status.SENT)

    @patch('tickets.sms.send_sms', side_effect=lambda to, body, status_callback=None: (True, 'SM' + to[-4:], None))
    @patch('langchain_openai.ChatOpenAI')
    def test_enabled_plan_send_not_held(self, mock_openai, mock_send):
        from .tasks import send_sms_campaign_task
        mock_openai.return_value = _fake_structured_llm()
        c = self._confirm_step(self._make_event_plan())
        send_sms_campaign_task.delay(str(c.id))   # enabled plan → sends
        c.refresh_from_db()
        self.assertEqual(c.status, SMSCampaign.Status.SENT)

    @patch('langchain_openai.ChatOpenAI')
    def test_overdue_detection_and_modal(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        c = self._confirm_step(plan)
        SMSCampaign.objects.filter(id=c.id).update(scheduled_at=timezone.now() - timedelta(days=1))
        plan.enabled = False
        plan.save(update_fields=['enabled'])
        resp = self.client.get(reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertEqual(len(resp.context['overdue_steps']), 1)
        self.assertContains(resp, 'id="overdueModal"')
        # An enabled plan (even past-due) shows no overdue warning — the cron just sends it.
        plan.enabled = True
        plan.save(update_fields=['enabled'])
        resp2 = self.client.get(reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertEqual(resp2.context['overdue_steps'], [])

    @patch('langchain_openai.ChatOpenAI')
    def test_overdue_skip_cancels_and_refunds(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        c = self._confirm_step(plan)
        self.org.refresh_from_db()
        after_charge = self.org.sms_credit_balance_cents
        SMSCampaign.objects.filter(id=c.id).update(scheduled_at=timezone.now() - timedelta(days=1))
        plan.enabled = False
        plan.save(update_fields=['enabled'])
        resp = self.client.post(
            reverse('tickets:sms_plan_overdue_action', kwargs={'pk': plan.id, 'step': 0}),
            {'action': 'skip'},
        )
        self.assertTrue(resp.json()['ok'])
        c.refresh_from_db()
        self.assertEqual(c.status, SMSCampaign.Status.CANCELED)
        self.org.refresh_from_db()
        self.assertGreater(self.org.sms_credit_balance_cents, after_charge)   # refunded

    @patch('langchain_openai.ChatOpenAI')
    def test_overdue_reschedule_updates_campaign_and_step(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        c = self._confirm_step(plan)
        SMSCampaign.objects.filter(id=c.id).update(scheduled_at=timezone.now() - timedelta(days=1))
        plan.enabled = False
        plan.save(update_fields=['enabled'])
        future = (timezone.now() + timedelta(days=5)).astimezone(
            self.org.get_timezone()).strftime('%Y-%m-%dT%H:%M')
        resp = self.client.post(
            reverse('tickets:sms_plan_overdue_action', kwargs={'pk': plan.id, 'step': 0}),
            {'action': 'reschedule', 'send_at': future},
        )
        self.assertTrue(resp.json()['ok'])
        c.refresh_from_db()
        self.assertEqual(c.status, SMSCampaign.Status.SCHEDULED)
        self.assertGreater(c.scheduled_at, timezone.now())
        plan.refresh_from_db()
        from datetime import datetime
        self.assertEqual(
            datetime.fromisoformat(plan.steps[0]['send_at']).strftime('%Y-%m-%dT%H:%M'), future)
        # A past reschedule is rejected.
        past = (timezone.now() - timedelta(days=1)).astimezone(
            self.org.get_timezone()).strftime('%Y-%m-%dT%H:%M')
        self.assertEqual(self.client.post(
            reverse('tickets:sms_plan_overdue_action', kwargs={'pk': plan.id, 'step': 0}),
            {'action': 'reschedule', 'send_at': past}).status_code, 400)

    @patch('tickets.sms.send_sms', side_effect=lambda to, body, status_callback=None: (True, 'SM', None))
    @patch('langchain_openai.ChatOpenAI')
    def test_overdue_send_now_forces_dispatch(self, mock_openai, mock_send):
        from .tasks import send_sms_campaign_task
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        c = self._confirm_step(plan)
        SMSCampaign.objects.filter(id=c.id).update(scheduled_at=timezone.now() - timedelta(days=1))
        plan.enabled = False
        plan.save(update_fields=['enabled'])
        with patch.object(send_sms_campaign_task, 'delay') as mock_delay:
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(
                    reverse('tickets:sms_plan_overdue_action', kwargs={'pk': plan.id, 'step': 0}),
                    {'action': 'send_now'},
                )
            self.assertTrue(resp.json()['ok'])
            mock_delay.assert_called_once()
            self.assertTrue(mock_delay.call_args.kwargs.get('force'))
        c.refresh_from_db()
        self.assertLessEqual(c.scheduled_at, timezone.now() + timedelta(seconds=5))

    @patch('langchain_openai.ChatOpenAI')
    def test_plan_list_shows_toggle_and_overdue_indicator(self, mock_openai):
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        c = self._confirm_step(plan)
        resp = self.client.get(reverse('tickets:sms_plan_list'))
        self.assertContains(resp, 'plan-enable-toggle')          # per-row switch
        self.assertNotContains(resp, 'bi-exclamation-triangle-fill')
        SMSCampaign.objects.filter(id=c.id).update(scheduled_at=timezone.now() - timedelta(days=1))
        plan.enabled = False
        plan.save(update_fields=['enabled'])
        resp2 = self.client.get(reverse('tickets:sms_plan_list'))
        self.assertContains(resp2, 'bi-exclamation-triangle-fill')   # overdue indicator
        self.assertContains(resp2, '>Disabled<')

    def test_completed_plan_hides_toggle_in_list(self):
        S = SMSCampaign.Status
        P = SMSCampaignPlan.Status
        done = self._plan_with_steps('Done', [S.SENT], P.SENT)
        live = self._plan_with_steps('Live', [S.SCHEDULED], P.SCHEDULED)
        resp = self.client.get(reverse('tickets:sms_plan_list'))
        # A finished (Sent) plan shows no enable/disable toggle; an active one still does.
        self.assertNotContains(resp, reverse('tickets:sms_plan_toggle_enabled', kwargs={'pk': done.id}))
        self.assertContains(resp, reverse('tickets:sms_plan_toggle_enabled', kwargs={'pk': live.id}))

    # --- Scheduled step → "move back to draft" (draft-before-delete) -----------

    @patch('langchain_openai.ChatOpenAI')
    def test_unschedule_step_reverts_to_draft_and_refunds(self, mock_openai):
        # Moving a scheduled step back to draft cancels + refunds its campaign and clears the
        # step's launch linkage so it's a true draft again.
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        c = self._confirm_step(plan)
        self.assertEqual(c.status, SMSCampaign.Status.SCHEDULED)
        self.org.refresh_from_db()
        after_charge = self.org.sms_credit_balance_cents

        resp = self.client.post(
            reverse('tickets:sms_plan_unschedule_step', kwargs={'pk': plan.id, 'step': 0}),
        )
        self.assertRedirects(resp, reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        c.refresh_from_db()
        self.assertEqual(c.status, SMSCampaign.Status.CANCELED)
        self.assertIsNone(c.plan_id)                      # detached from the plan
        self.org.refresh_from_db()
        self.assertGreater(self.org.sms_credit_balance_cents, after_charge)  # refunded
        plan.refresh_from_db()
        self.assertIsNone(plan.steps[0]['launched_campaign_id'])
        self.assertIsNone(plan.steps[0].get('launched_at'))
        # All steps back to draft → plan status recomputed to Draft.
        self.assertEqual(plan.status, SMSCampaignPlan.Status.DRAFT)

    @patch('langchain_openai.ChatOpenAI')
    def test_unschedule_errors_when_step_is_draft(self, mock_openai):
        # A step that was never launched can't be "moved back to draft".
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        resp = self.client.post(
            reverse('tickets:sms_plan_unschedule_step', kwargs={'pk': plan.id, 'step': 0}),
        )
        self.assertRedirects(resp, reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertEqual(SMSCampaign.objects.filter(organization=self.org).count(), 0)
        plan.refresh_from_db()
        self.assertIsNone(plan.steps[0].get('launched_campaign_id'))

    @patch('langchain_openai.ChatOpenAI')
    def test_unschedule_errors_when_step_is_sent(self, mock_openai):
        # A sent step is history — unschedule leaves the campaign and linkage untouched.
        mock_openai.return_value = _fake_structured_llm()
        plan = self._make_event_plan()
        campaign = SMSCampaign.objects.create(
            organization=self.org, name='sent', body=plan.steps[0]['body'],
            status=SMSCampaign.Status.SENT)
        plan.steps[0]['launched_campaign_id'] = str(campaign.id)
        plan.steps[0]['launched_at'] = timezone.now().isoformat()
        plan.save(update_fields=['steps'])

        resp = self.client.post(
            reverse('tickets:sms_plan_unschedule_step', kwargs={'pk': plan.id, 'step': 0}),
        )
        self.assertRedirects(resp, reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, SMSCampaign.Status.SENT)
        plan.refresh_from_db()
        self.assertEqual(plan.steps[0]['launched_campaign_id'], str(campaign.id))

    def test_remove_step_blocked_when_scheduled(self):
        # A scheduled step must be moved back to draft before it can be deleted.
        plan = self._plan_with_steps(
            'Sched', [SMSCampaign.Status.SCHEDULED, None, None],
            SMSCampaignPlan.Status.IN_PROGRESS)
        cid = plan.steps[0]['launched_campaign_id']

        resp = self.client.post(
            reverse('tickets:sms_plan_remove_step', kwargs={'pk': plan.id, 'step': 0}),
        )
        self.assertRedirects(resp, reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        plan.refresh_from_db()
        self.assertEqual(len(plan.steps), 3)                       # not popped
        self.assertEqual(plan.steps[0]['launched_campaign_id'], cid)
        self.assertEqual(
            SMSCampaign.objects.get(id=cid).status, SMSCampaign.Status.SCHEDULED)  # not canceled

    def test_render_scheduled_step_shows_unschedule_not_remove(self):
        # A scheduled step shows the "move back to draft" control instead of the trash icon;
        # draft steps still show the trash.
        plan = self._plan_with_steps(
            'Sched', [SMSCampaign.Status.SCHEDULED, None],
            SMSCampaignPlan.Status.IN_PROGRESS)
        resp = self.client.get(reverse('tickets:sms_plan_detail', kwargs={'pk': plan.id}))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context['steps'][0]['is_scheduled'])
        self.assertFalse(resp.context['steps'][1].get('is_scheduled'))
        self.assertContains(
            resp, reverse('tickets:sms_plan_unschedule_step', kwargs={'pk': plan.id, 'step': 0}))
        self.assertNotContains(
            resp, reverse('tickets:sms_plan_remove_step', kwargs={'pk': plan.id, 'step': 0}))
        self.assertContains(
            resp, reverse('tickets:sms_plan_remove_step', kwargs={'pk': plan.id, 'step': 1}))


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
