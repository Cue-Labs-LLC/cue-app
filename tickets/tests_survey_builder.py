"""Tests for the survey builder: parsing, freeze-on-send, builder CRUD,
public choice submission, choice-tally aggregation, and integrity."""

import json
from datetime import date
from decimal import Decimal

from django.test import TestCase, Client
from django.urls import reverse
from django.http import QueryDict
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from django.utils import timezone

from .models import (
    Organization, UserProfile, Venue, Event, Customer, TicketOrder,
    SurveyQuestion, SurveyQuestionOption, SurveyInvitation, SurveyResponse,
    SurveyAnswer, SurveyAnswerOption,
)
from .views import _parse_survey_answer, _copy_question, _compute_event_stats


def _qd(pairs):
    """Build a QueryDict from a list of (key, value) pairs (repeats allowed)."""
    qd = QueryDict('', mutable=True)
    for k, v in pairs:
        qd.appendlist(k, v)
    return qd


def _opt_post(labels, prefix='options', initial=0):
    data = {
        f'{prefix}-TOTAL_FORMS': str(len(labels)),
        f'{prefix}-INITIAL_FORMS': str(initial),
        f'{prefix}-MIN_NUM_FORMS': '0',
        f'{prefix}-MAX_NUM_FORMS': '1000',
    }
    for i, label in enumerate(labels):
        data[f'{prefix}-{i}-label'] = label
        data[f'{prefix}-{i}-position'] = str(i)
    return data


# ---------------------------------------------------------------------------
# _parse_survey_answer (unit)
# ---------------------------------------------------------------------------

class ParseSurveyAnswerTests(TestCase):
    def _q(self, qtype, required=False, options=None):
        q = SurveyQuestion.objects.create(question_text='Q', question_type=qtype, is_required=required)
        for i, label in enumerate(options or []):
            SurveyQuestionOption.objects.create(question=q, label=label, position=i)
        return q

    def test_star_valid(self):
        q = self._q('star_rating')
        data, err = _parse_survey_answer(q, _qd([(f'question_{q.id}', '4')]))
        self.assertIsNone(err)
        self.assertEqual(data['star_rating'], 4)

    def test_star_out_of_range(self):
        q = self._q('star_rating')
        _, err = _parse_survey_answer(q, _qd([(f'question_{q.id}', '9')]))
        self.assertIsNotNone(err)

    def test_star_non_int(self):
        q = self._q('star_rating')
        _, err = _parse_survey_answer(q, _qd([(f'question_{q.id}', 'x')]))
        self.assertIsNotNone(err)

    def test_nps_valid_and_range(self):
        q = self._q('nps')
        data, err = _parse_survey_answer(q, _qd([(f'question_{q.id}', '10')]))
        self.assertIsNone(err)
        self.assertEqual(data['nps_score'], 10)
        _, err = _parse_survey_answer(q, _qd([(f'question_{q.id}', '11')]))
        self.assertIsNotNone(err)

    def test_text_and_required_empty(self):
        q = self._q('text', required=True)
        _, err = _parse_survey_answer(q, _qd([(f'question_{q.id}', '')]))
        self.assertIsNotNone(err)
        data, err = _parse_survey_answer(q, _qd([(f'question_{q.id}', 'hello')]))
        self.assertIsNone(err)
        self.assertEqual(data['text_answer'], 'hello')

    def test_single_select_valid_and_bogus(self):
        q = self._q('single_select', options=['A', 'B'])
        good = str(q.options.first().id)
        data, err = _parse_survey_answer(q, _qd([(f'question_{q.id}', good)]))
        self.assertIsNone(err)
        self.assertEqual(data['selected_option_ids'], [good])
        _, err = _parse_survey_answer(q, _qd([(f'question_{q.id}', 'deadbeef')]))
        self.assertIsNotNone(err)

    def test_multi_select_multiple_and_bogus(self):
        q = self._q('multi_select', options=['A', 'B', 'C'])
        ids = [str(o.id) for o in q.options.all()[:2]]
        data, err = _parse_survey_answer(q, _qd([(f'question_{q.id}', ids[0]), (f'question_{q.id}', ids[1])]))
        self.assertIsNone(err)
        self.assertEqual(set(data['selected_option_ids']), set(ids))
        _, err = _parse_survey_answer(q, _qd([(f'question_{q.id}', ids[0]), (f'question_{q.id}', 'nope')]))
        self.assertIsNotNone(err)

    def test_multi_select_required_empty(self):
        q = self._q('multi_select', required=True, options=['A'])
        _, err = _parse_survey_answer(q, _qd([]))
        self.assertIsNotNone(err)


# ---------------------------------------------------------------------------
# _copy_question + model integrity
# ---------------------------------------------------------------------------

class CopyQuestionTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Copy Org', slug='copy-org')

    def test_copies_row_and_options_with_overrides(self):
        src = SurveyQuestion.objects.create(
            organization=self.org, question_text='Pick one', question_type='single_select', position=2,
        )
        SurveyQuestionOption.objects.create(question=src, label='X', position=0)
        SurveyQuestionOption.objects.create(question=src, label='Y', position=1)
        venue = Venue.objects.create(organization=self.org, name='V', city='C')
        event = Event.objects.create(organization=self.org, name='E', venue=venue, start_date=date(2025, 1, 1))

        new_q = _copy_question(src, event=event)
        self.assertNotEqual(new_q.id, src.id)
        self.assertEqual(new_q.event_id, event.id)
        self.assertEqual(new_q.question_text, 'Pick one')
        self.assertEqual(list(new_q.options.values_list('label', flat=True)), ['X', 'Y'])


class SurveyAnswerCleanTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Clean Org', slug='clean-org')
        self.venue = Venue.objects.create(organization=self.org, name='V', city='C')
        self.event = Event.objects.create(organization=self.org, name='E', venue=self.venue, start_date=date(2025, 1, 1))
        self.customer = Customer.objects.create(organization=self.org, email='c@e.com', name='C')
        inv = SurveyInvitation.objects.create(organization=self.org, event=self.event, customer=self.customer, email='c@e.com')
        self.response = SurveyResponse.objects.create(organization=self.org, event=self.event, customer=self.customer, invitation=inv)

    def test_rejects_mismatched_field_family(self):
        q = SurveyQuestion.objects.create(organization=self.org, question_text='Stars', question_type='star_rating')
        ans = SurveyAnswer(response=self.response, question=q, star_rating=4, nps_score=9)
        with self.assertRaises(ValidationError):
            ans.full_clean(exclude=['selected_options'])

    def test_rejects_foreign_option(self):
        q1 = SurveyQuestion.objects.create(organization=self.org, question_text='Q1', question_type='single_select')
        q2 = SurveyQuestion.objects.create(organization=self.org, question_text='Q2', question_type='single_select')
        opt_other = SurveyQuestionOption.objects.create(question=q2, label='Other', position=0)
        ans = SurveyAnswer.objects.create(response=self.response, question=q1)
        link = SurveyAnswerOption(answer=ans, option=opt_other, question=q1)
        with self.assertRaises(ValidationError):
            link.full_clean()


# ---------------------------------------------------------------------------
# Shared base for view tests
# ---------------------------------------------------------------------------

class _SurveyViewBase(TestCase):
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Org A', slug='org-a')
        self.other_org = Organization.objects.create(name='Org B', slug='org-b')
        self.user = User.objects.create_user(username='host', email='host@a.com', password='pw12345')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.venue = Venue.objects.create(organization=self.org, name='V', city='C')
        self.event = Event.objects.create(organization=self.org, name='Night', venue=self.venue, start_date=date(2025, 8, 1))
        self.customer = Customer.objects.create(organization=self.org, email='guest@e.com', name='Guest')

    def _login_host(self):
        self.client.login(username='host@a.com', password='pw12345')
        self.client.get(reverse('tickets:home'))

    def _attendee_order(self):
        TicketOrder.objects.create(
            customer=self.customer, event=self.event,
            order_number='ORD-1', order_date=timezone.now(), total_amount=Decimal('10.00'),
        )


# ---------------------------------------------------------------------------
# send_survey freeze (regression)
# ---------------------------------------------------------------------------

class SendSurveyFreezeTests(_SurveyViewBase):
    def test_send_clones_effective_set_to_event_scope(self):
        self._login_host()
        self._attendee_order()
        # Org-default question — the effective set for the event.
        SurveyQuestion.objects.create(organization=self.org, question_text='Org Q', question_type='nps', position=1)
        self.assertFalse(SurveyQuestion.objects.filter(event=self.event).exists())

        resp = self.client.post(reverse('tickets:send_survey', args=[self.event.id]))
        self.assertEqual(resp.status_code, 302)

        frozen = SurveyQuestion.objects.filter(event=self.event)
        self.assertEqual(frozen.count(), 1)
        self.assertEqual(frozen.first().question_text, 'Org Q')
        self.assertTrue(SurveyInvitation.objects.filter(event=self.event, customer=self.customer).exists())


# ---------------------------------------------------------------------------
# Public form submission (regression + choice)
# ---------------------------------------------------------------------------

class PublicSurveySubmitTests(_SurveyViewBase):
    def _invitation(self):
        return SurveyInvitation.objects.create(
            organization=self.org, event=self.event, customer=self.customer, email='guest@e.com',
        )

    def test_star_nps_text_submit_regression(self):
        q_star = SurveyQuestion.objects.create(event=self.event, organization=self.org, question_text='Stars', question_type='star_rating', position=1)
        q_text = SurveyQuestion.objects.create(event=self.event, organization=self.org, question_text='Notes', question_type='text', position=2)
        inv = self._invitation()
        url = reverse('tickets:survey_form', args=[inv.token])
        self.assertEqual(self.client.get(url).status_code, 200)
        resp = self.client.post(url, {f'question_{q_star.id}': '5', f'question_{q_text.id}': 'Loved it'})
        self.assertEqual(resp.status_code, 302)
        answer = SurveyAnswer.objects.get(question=q_star)
        self.assertEqual(answer.star_rating, 5)
        self.assertEqual(SurveyAnswer.objects.get(question=q_text).text_answer, 'Loved it')

    def test_multi_select_saves_through_rows(self):
        q = SurveyQuestion.objects.create(event=self.event, organization=self.org, question_text='Pick', question_type='multi_select', position=1)
        a = SurveyQuestionOption.objects.create(question=q, label='A', position=0)
        b = SurveyQuestionOption.objects.create(question=q, label='B', position=1)
        SurveyQuestionOption.objects.create(question=q, label='C', position=2)
        inv = self._invitation()
        url = reverse('tickets:survey_form', args=[inv.token])
        resp = self.client.post(url, {f'question_{q.id}': [str(a.id), str(b.id)]})
        self.assertEqual(resp.status_code, 302)
        answer = SurveyAnswer.objects.get(question=q)
        self.assertEqual(set(answer.selected_options.values_list('label', flat=True)), {'A', 'B'})
        self.assertEqual(SurveyAnswerOption.objects.filter(answer=answer).count(), 2)

    def test_tampered_option_rejected(self):
        q = SurveyQuestion.objects.create(event=self.event, organization=self.org, question_text='Pick', question_type='single_select', is_required=True, position=1)
        SurveyQuestionOption.objects.create(question=q, label='A', position=0)
        inv = self._invitation()
        url = reverse('tickets:survey_form', args=[inv.token])
        # Foreign org's option id.
        foreign_q = SurveyQuestion.objects.create(organization=self.other_org, question_text='X', question_type='single_select')
        foreign_opt = SurveyQuestionOption.objects.create(question=foreign_q, label='Z', position=0)
        resp = self.client.post(url, {f'question_{q.id}': str(foreign_opt.id)})
        self.assertEqual(resp.status_code, 200)  # re-rendered with error
        self.assertFalse(SurveyResponse.objects.filter(event=self.event).exists())


# ---------------------------------------------------------------------------
# Builder CRUD + lock + authz
# ---------------------------------------------------------------------------

class SurveyBuilderTests(_SurveyViewBase):
    def _save(self, payload, args=None):
        return self.client.post(
            reverse('tickets:survey_question_save', args=args or []),
            data=json.dumps(payload), content_type='application/json',
        )

    def test_create_choice_question_with_options(self):
        self._login_host()
        resp = self._save({
            'question_text': 'Favourite set?', 'question_type': 'single_select',
            'is_required': True, 'options': [{'label': 'Opener'}, {'label': 'Headliner'}],
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['ok'])
        q = SurveyQuestion.objects.get(organization=self.org, event__isnull=True, question_text='Favourite set?')
        self.assertEqual(q.question_type, 'single_select')
        self.assertEqual(list(q.options.values_list('label', flat=True)), ['Opener', 'Headliner'])

    def test_create_choice_without_options_rejected(self):
        self._login_host()
        resp = self._save({'question_text': 'Pick', 'question_type': 'multi_select', 'options': []})
        self.assertEqual(resp.status_code, 422)
        self.assertFalse(resp.json()['ok'])
        self.assertFalse(SurveyQuestion.objects.filter(question_text='Pick').exists())

    def test_update_replaces_options(self):
        self._login_host()
        created = self._save({
            'question_text': 'Pick', 'question_type': 'single_select',
            'options': [{'label': 'A'}, {'label': 'B'}],
        }).json()['question']
        resp = self._save({
            'question_text': 'Pick one', 'question_type': 'single_select',
            'options': [{'label': 'X'}],
        }, args=[created['id']])
        self.assertTrue(resp.json()['ok'])
        q = SurveyQuestion.objects.get(id=created['id'])
        self.assertEqual(q.question_text, 'Pick one')
        self.assertEqual(list(q.options.values_list('label', flat=True)), ['X'])

    def test_single_nps_enforced(self):
        self._login_host()
        SurveyQuestion.objects.create(organization=self.org, question_text='First NPS', question_type='nps', position=1)
        resp = self._save({'question_text': 'Second NPS', 'question_type': 'nps'})
        self.assertEqual(resp.status_code, 422)
        self.assertFalse(resp.json()['ok'])
        self.assertEqual(SurveyQuestion.objects.filter(organization=self.org, question_type='nps').count(), 1)

    def test_locked_event_rejects_save(self):
        self._login_host()
        q = SurveyQuestion.objects.create(event=self.event, organization=self.org, question_text='Locked Q', question_type='text', position=1)
        SurveyInvitation.objects.create(organization=self.org, event=self.event, customer=self.customer, email='guest@e.com')  # locks
        resp = self.client.post(
            reverse('tickets:event_survey_question_save', args=[self.event.id, q.id]),
            data=json.dumps({'question_text': 'Changed', 'question_type': 'text'}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 403)
        q.refresh_from_db()
        self.assertEqual(q.question_text, 'Locked Q')

    def test_delete_json(self):
        self._login_host()
        q = SurveyQuestion.objects.create(organization=self.org, question_text='Bye', question_type='text', position=1)
        resp = self.client.post(reverse('tickets:survey_question_delete', args=[q.id]))
        self.assertTrue(resp.json()['ok'])
        self.assertFalse(SurveyQuestion.objects.filter(id=q.id).exists())

    def test_customize_clones_then_reset_clears(self):
        self._login_host()
        SurveyQuestion.objects.create(organization=self.org, question_text='Org Q', question_type='text', position=1)
        self.client.post(reverse('tickets:event_survey_customize', args=[self.event.id]))
        self.assertEqual(SurveyQuestion.objects.filter(event=self.event).count(), 1)
        self.client.post(reverse('tickets:event_survey_reset', args=[self.event.id]))
        self.assertEqual(SurveyQuestion.objects.filter(event=self.event).count(), 0)

    def test_reorder_swaps_positions(self):
        self._login_host()
        q1 = SurveyQuestion.objects.create(organization=self.org, question_text='Q1', question_type='text', position=0)
        q2 = SurveyQuestion.objects.create(organization=self.org, question_text='Q2', question_type='text', position=1)
        resp = self.client.post(
            reverse('tickets:survey_reorder'),
            data=json.dumps({'question_id': str(q2.id), 'direction': 'up'}),
            content_type='application/json',
        )
        self.assertTrue(resp.json()['ok'])
        q1.refresh_from_db(); q2.refresh_from_db()
        self.assertEqual(q2.position, 0)
        self.assertEqual(q1.position, 1)

    def test_require_host_blocks_doorman(self):
        doorman = User.objects.create_user(username='door', email='door@a.com', password='pw12345')
        UserProfile.objects.create(user=doorman, organization=self.org, org_role=UserProfile.OrgRole.DOORMAN)
        self.client.login(username='door@a.com', password='pw12345')
        self.client.get(reverse('tickets:home'))
        resp = self.client.get(reverse('tickets:survey_builder'))
        self.assertIn(resp.status_code, (302, 403))

    def test_cross_org_event_404(self):
        self._login_host()
        other_venue = Venue.objects.create(organization=self.other_org, name='V2', city='C2')
        other_event = Event.objects.create(organization=self.other_org, name='Other', venue=other_venue, start_date=date(2025, 9, 1))
        resp = self.client.get(reverse('tickets:event_survey_builder', args=[other_event.id]))
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Live builder preview
# ---------------------------------------------------------------------------

class SurveyPreviewTests(_SurveyViewBase):
    def test_get_renders_saved_questions_with_preview_guards(self):
        self._login_host()
        q = SurveyQuestion.objects.create(organization=self.org, question_text='Rate the night', question_type='single_select', position=1)
        SurveyQuestionOption.objects.create(question=q, label='Amazing', position=0)
        resp = self.client.get(reverse('tickets:survey_preview'))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('Rate the night', html)
        self.assertIn('Amazing', html)
        self.assertIn('>Preview<', html)          # preview ribbon
        self.assertIn('disabled', html)            # submit disabled
        self.assertNotIn('luckyorange', html)      # analytics skipped

    def test_org_scope_uses_placeholder_event_name(self):
        self._login_host()
        SurveyQuestion.objects.create(organization=self.org, question_text='Q', question_type='text', position=1)
        html = self.client.get(reverse('tickets:survey_preview')).content.decode()
        self.assertIn('Your event', html)

    def test_event_scope_shows_event_name(self):
        self._login_host()
        SurveyQuestion.objects.create(organization=self.org, question_text='Q', question_type='text', position=1)
        html = self.client.get(reverse('tickets:event_survey_preview', args=[self.event.id])).content.decode()
        self.assertIn(self.event.name, html)

    def test_post_renders_draft(self):
        self._login_host()
        draft = {'questions': [{'question_text': 'Live draft Q', 'question_type': 'nps', 'is_required': True, 'options': []}]}
        resp = self.client.post(reverse('tickets:survey_preview'), data=json.dumps(draft), content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('Live draft Q', html)
        self.assertIn('nps-group', html)

    def test_post_draft_choice_renders_options(self):
        self._login_host()
        draft = {'questions': [{'question_text': 'Pick', 'question_type': 'multi_select', 'options': [{'label': 'Red'}, {'label': 'Blue'}]}]}
        html = self.client.post(reverse('tickets:survey_preview'), data=json.dumps(draft), content_type='application/json').content.decode()
        self.assertIn('Red', html)
        self.assertIn('Blue', html)
        self.assertIn('choice-group', html)

    def test_malformed_json_400(self):
        self._login_host()
        resp = self.client.post(reverse('tickets:survey_preview'), data='{bad', content_type='application/json')
        self.assertEqual(resp.status_code, 400)

    def test_requires_host(self):
        doorman = User.objects.create_user(username='door2', email='door2@a.com', password='pw12345')
        UserProfile.objects.create(user=doorman, organization=self.org, org_role=UserProfile.OrgRole.DOORMAN)
        self.client.login(username='door2@a.com', password='pw12345')
        self.client.get(reverse('tickets:home'))
        resp = self.client.get(reverse('tickets:survey_preview'))
        self.assertIn(resp.status_code, (302, 403))

    def test_cross_org_event_404(self):
        self._login_host()
        ov = Venue.objects.create(organization=self.other_org, name='OV', city='OC')
        oe = Event.objects.create(organization=self.other_org, name='Other', venue=ov, start_date=date(2025, 9, 1))
        resp = self.client.get(reverse('tickets:event_survey_preview', args=[oe.id]))
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Choice tally aggregation + response detail
# ---------------------------------------------------------------------------

class ChoiceTallyTests(_SurveyViewBase):
    _seq = 0

    def _response_with_choice(self, question, option):
        ChoiceTallyTests._seq += 1
        inv = SurveyInvitation.objects.create(
            organization=self.org, event=self.event,
            customer=Customer.objects.create(
                organization=self.org, email=f'resp{ChoiceTallyTests._seq}@e.com', name=f'R{ChoiceTallyTests._seq}',
            ),
            email='x@e.com',
        )
        resp = SurveyResponse.objects.create(organization=self.org, event=self.event, customer=inv.customer, invitation=inv)
        ans = SurveyAnswer.objects.create(response=resp, question=question)
        SurveyAnswerOption.objects.create(answer=ans, option=option, question=question)
        return resp

    def test_choice_breakdown_counts(self):
        q = SurveyQuestion.objects.create(event=self.event, organization=self.org, question_text='Pick', question_type='single_select', position=1)
        a = SurveyQuestionOption.objects.create(question=q, label='A', position=0)
        b = SurveyQuestionOption.objects.create(question=q, label='B', position=1)
        self._response_with_choice(q, a)
        self._response_with_choice(q, a)
        self._response_with_choice(q, b)
        stats = _compute_event_stats(self.event)
        breakdowns = stats['survey_results']['choice_breakdowns']
        self.assertEqual(len(breakdowns), 1)
        counts = {o['label']: o['count'] for o in breakdowns[0]['options']}
        self.assertEqual(counts, {'A': 2, 'B': 1})

    def test_response_detail_renders_choice_labels(self):
        self._login_host()
        q = SurveyQuestion.objects.create(event=self.event, organization=self.org, question_text='Pick', question_type='multi_select', position=1)
        a = SurveyQuestionOption.objects.create(question=q, label='A', position=0)
        b = SurveyQuestionOption.objects.create(question=q, label='B', position=1)
        resp_obj = self._response_with_choice(q, a)
        SurveyAnswerOption.objects.create(answer=resp_obj.answers.first(), option=b, question=q)
        out = self.client.get(reverse('tickets:event_survey_response_detail', args=[self.event.id, 'internal', resp_obj.id]))
        self.assertEqual(out.status_code, 200)
        items = {it['question']: it['answer'] for it in out.json()['items']}
        self.assertEqual(set(items['Pick'].split(', ')), {'A', 'B'})
