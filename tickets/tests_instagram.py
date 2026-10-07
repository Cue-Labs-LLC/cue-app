"""Tests for the Instagram DM support agent (Phases 0–2).

Covers the data-model foundations (OrgFAQ, InstagramConversation, InstagramMessage,
the Organization integration fields, the AITokenUsage feature), the per-org FAQ editor
(Phase 1), and the offline answer pipeline (Phase 2): the customer-safe tool surface
(allowlist pin, output scrubbing, visibility), the ReAct answer agent, and the
escalation classifier + auto-send gate.
"""

import hashlib
import hmac
import json
from decimal import Decimal
from io import StringIO
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import OrgFAQForm
from .models import (
    AITokenUsage,
    InstagramConversation,
    InstagramMessage,
    OrganizationMembership,
    OrgFAQ,
    Organization,
    UserProfile,
)
from .services.instagram import (
    AnswerResult,
    EscalationDecision,
    InstagramSupportAgentService,
    build_ig_tools,
    classify_escalation,
    decide_autosend,
)
from .services.instagram.tools import (
    _find_event, _get_contact_info, _get_faq, _list_upcoming_events,
)
from .services.instagram.evaluation import (
    DisclosureVerdict, grade_escalation, grade_grounded, grade_tool,
    judge_private_disclosure, load_cases,
)


class OrgFAQModelTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='FAQ Org', slug='faq-org')

    def _faq(self, question='When does it start?', **kwargs):
        return OrgFAQ.objects.create(
            organization=self.org,
            question=question,
            answer=kwargs.pop('answer', 'Doors at 9pm.'),
            **kwargs,
        )

    def test_is_published_defaults_true(self):
        faq = self._faq()
        self.assertTrue(faq.is_published)
        self.assertEqual(faq.sort_order, 0)
        self.assertEqual(faq.topic, '')

    def test_soft_delete_then_hard_delete(self):
        faq = self._faq()
        faq.delete()
        faq.refresh_from_db()
        self.assertIsNotNone(faq.deleted_at)
        # Soft-deleted row is still present in the default queryset.
        self.assertTrue(OrgFAQ.objects.filter(pk=faq.pk).exists())
        faq.hard_delete()
        self.assertFalse(OrgFAQ.objects.filter(pk=faq.pk).exists())

    def test_default_ordering_by_sort_order_then_created(self):
        a = self._faq(question='A', sort_order=2)
        b = self._faq(question='B', sort_order=1)
        c = self._faq(question='C', sort_order=1)
        ordered = list(OrgFAQ.objects.all())
        # sort_order asc first (b, c before a); within equal sort_order, created_at asc.
        self.assertEqual(ordered, [b, c, a])


class InstagramConversationModelTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='IG Org', slug='ig-org')
        self.other_org = Organization.objects.create(name='Other IG Org', slug='other-ig-org')

    def test_status_defaults_open(self):
        conv = InstagramConversation.objects.create(organization=self.org, ig_user_id='igsid-1')
        self.assertEqual(conv.status, 'open')
        self.assertEqual(conv.ig_username, '')
        self.assertIsNone(conv.customer)
        self.assertIsNotNone(conv.last_message_at)

    def test_unique_org_ig_user(self):
        InstagramConversation.objects.create(organization=self.org, ig_user_id='dup')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                InstagramConversation.objects.create(organization=self.org, ig_user_id='dup')

    def test_same_ig_user_allowed_across_orgs(self):
        InstagramConversation.objects.create(organization=self.org, ig_user_id='shared')
        # Must not raise — uniqueness is scoped per organization.
        conv = InstagramConversation.objects.create(organization=self.other_org, ig_user_id='shared')
        self.assertEqual(conv.organization, self.other_org)


class InstagramMessageModelTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='IG Msg Org', slug='ig-msg-org')
        self.conv = InstagramConversation.objects.create(organization=self.org, ig_user_id='igsid-9')

    def _msg(self, **kwargs):
        return InstagramMessage.objects.create(
            conversation=self.conv,
            organization=self.org,
            direction=kwargs.pop('direction', 'inbound'),
            author=kwargs.pop('author', 'customer'),
            content=kwargs.pop('content', 'hi'),
            **kwargs,
        )

    def test_defaults(self):
        msg = self._msg()
        self.assertEqual(msg.status, 'received')
        self.assertEqual(msg.token_count, 0)
        self.assertIsNone(msg.confidence)
        self.assertEqual(msg.escalation_category, '')
        self.assertEqual(msg.provider_message_id, '')

    def test_provider_message_id_persists(self):
        self._msg(provider_message_id='mid.abc123')
        self.assertTrue(
            InstagramMessage.objects.filter(provider_message_id='mid.abc123').exists()
        )

    def test_duplicate_provider_message_id_rejected(self):
        # D5: DB-enforced idempotency for retried webhook/task deliveries.
        self._msg(provider_message_id='mid.dup')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._msg(provider_message_id='mid.dup')

    def test_blank_provider_message_ids_do_not_collide(self):
        # Partial constraint: outbound drafts (empty id) must coexist.
        self._msg(direction='outbound', author='agent', status='pending_review')
        self._msg(direction='outbound', author='agent', status='pending_review')
        self.assertEqual(self.conv.messages.filter(provider_message_id='').count(), 2)

    def test_save_coerces_organization_from_conversation(self):
        # D6: a mismatched organization is overwritten with the conversation's.
        other = Organization.objects.create(name='Wrong Org', slug='wrong-org')
        msg = InstagramMessage.objects.create(
            conversation=self.conv,
            organization=other,
            direction='inbound',
            author='customer',
            content='hi',
        )
        msg.refresh_from_db()
        self.assertEqual(msg.organization_id, self.conv.organization_id)

    def test_delete_conversation_cascades(self):
        self._msg()
        self._msg(direction='outbound', author='agent', status='auto_sent')
        self.assertEqual(self.conv.messages.count(), 2)
        self.conv.delete()
        self.assertEqual(InstagramMessage.objects.filter(organization=self.org).count(), 0)

    def test_ordering_by_created_at(self):
        first = self._msg(content='first')
        second = self._msg(content='second')
        self.assertEqual(list(self.conv.messages.all()), [first, second])


class OrganizationInstagramFieldsTests(TestCase):
    def test_instagram_fields_default_empty(self):
        org = Organization.objects.create(name='Defaults Org', slug='defaults-org')
        self.assertEqual(org.instagram_page_access_token, '')
        self.assertEqual(org.instagram_business_account_id, '')
        self.assertEqual(org.instagram_page_id, '')
        self.assertEqual(org.instagram_username, '')
        self.assertIsNone(org.instagram_token_expires_at)
        self.assertFalse(org.instagram_support_agent_enabled)

    def test_instagram_business_account_id_unique_when_set(self):
        # D11: inbound routing keys on this id, so it must map to one org.
        Organization.objects.create(
            name='IG Acct Org', slug='ig-acct-org',
            instagram_business_account_id='17841400000000000',
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Organization.objects.create(
                    name='Dup IG Acct Org', slug='dup-ig-acct-org',
                    instagram_business_account_id='17841400000000000',
                )

    def test_blank_instagram_business_account_ids_do_not_collide(self):
        # Partial constraint: unconnected orgs (default '') must coexist.
        # (A full unique constraint would raise on the second blank create.)
        Organization.objects.create(name='Unconnected A', slug='unconnected-a')
        Organization.objects.create(name='Unconnected B', slug='unconnected-b')
        self.assertEqual(
            Organization.objects.filter(
                slug__in=['unconnected-a', 'unconnected-b'],
                instagram_business_account_id='',
            ).count(),
            2,
        )


class AITokenUsageFeatureTests(TestCase):
    def test_ig_support_agent_feature_choice_exists(self):
        self.assertEqual(AITokenUsage.FEATURE_IG_SUPPORT_AGENT, 'ig_support_agent')
        self.assertIn(
            AITokenUsage.FEATURE_IG_SUPPORT_AGENT,
            dict(AITokenUsage.FEATURE_CHOICES),
        )

    def test_can_record_usage_row_with_null_user(self):
        org = Organization.objects.create(name='Meter Org', slug='meter-org')
        usage = AITokenUsage.objects.create(
            organization=org,
            feature=AITokenUsage.FEATURE_IG_SUPPORT_AGENT,
            model_name='gpt-4o',
            user=None,
        )
        self.assertEqual(usage.feature, 'ig_support_agent')
        self.assertIsNone(usage.user)


# ---------------------------------------------------------------------------
# Phase 1 — per-org FAQ editor (settings) CRUD + reorder + access control
# ---------------------------------------------------------------------------


class _FAQViewTestBase(TestCase):
    """Shared setup: an org with an admin and a non-admin (host) member."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='FAQ View Org', slug='faq-view-org')
        self.other_org = Organization.objects.create(name='Other FAQ Org', slug='other-faq-org')

        self.admin_user = User.objects.create_user(
            username='faqadmin', email='faqadmin@example.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.admin_user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        OrganizationMembership.objects.create(
            user=self.admin_user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )

        self.host_user = User.objects.create_user(
            username='faqhost', email='faqhost@example.com', password='testpass123',
        )
        UserProfile.objects.create(
            user=self.host_user, organization=self.org, org_role=UserProfile.OrgRole.HOST,
        )
        OrganizationMembership.objects.create(
            user=self.host_user, organization=self.org, org_role=UserProfile.OrgRole.HOST,
        )

    def _login_admin(self):
        self.client.login(username='faqadmin@example.com', password='testpass123')
        # Seed the session _org_id so @require_org resolves the active org.
        self.client.get(reverse('tickets:home'))

    def _login_host(self):
        self.client.login(username='faqhost@example.com', password='testpass123')
        self.client.get(reverse('tickets:home'))

    def _faq(self, organization=None, question='When do doors open?', **kwargs):
        return OrgFAQ.objects.create(
            organization=organization or self.org,
            question=question,
            answer=kwargs.pop('answer', 'Doors at 9pm.'),
            **kwargs,
        )


class FAQAccessControlTests(_FAQViewTestBase):
    def test_non_admin_forbidden_on_all_views(self):
        self._login_host()
        faq = self._faq()
        urls = [
            reverse('tickets:instagram_faq_list'),
            reverse('tickets:instagram_faq_create'),
            reverse('tickets:instagram_faq_edit', args=[faq.id]),
            reverse('tickets:instagram_faq_delete', args=[faq.id]),
        ]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 403, url)
        self.assertEqual(
            self.client.post(reverse('tickets:instagram_faq_reorder')).status_code,
            403,
        )

    def test_admin_can_open_list(self):
        self._login_admin()
        response = self.client.get(reverse('tickets:instagram_faq_list'))
        self.assertEqual(response.status_code, 200)


class FAQCrudTests(_FAQViewTestBase):
    def test_create_scopes_to_request_org(self):
        self._login_admin()
        response = self.client.post(
            reverse('tickets:instagram_faq_create'),
            {'question': 'Is it sold out?', 'answer': 'Check the ticket link.',
             'topic': 'tickets', 'is_published': 'on', 'sort_order': '0'},
        )
        self.assertRedirects(response, reverse('tickets:instagram_faq_list'))
        faq = OrgFAQ.objects.get(question='Is it sold out?')
        self.assertEqual(faq.organization, self.org)

    def test_edit_updates_fields(self):
        self._login_admin()
        faq = self._faq()
        self.client.post(
            reverse('tickets:instagram_faq_edit', args=[faq.id]),
            {'question': 'Updated?', 'answer': 'Yes updated.',
             'topic': '', 'is_published': 'on', 'sort_order': '3'},
        )
        faq.refresh_from_db()
        self.assertEqual(faq.question, 'Updated?')
        self.assertEqual(faq.sort_order, 3)

    def test_delete_is_soft_and_leaves_list(self):
        self._login_admin()
        faq = self._faq()
        self.client.post(reverse('tickets:instagram_faq_delete', args=[faq.id]))
        faq.refresh_from_db()
        self.assertIsNotNone(faq.deleted_at)  # soft delete
        response = self.client.get(reverse('tickets:instagram_faq_list'))
        self.assertNotIn(faq, response.context['faqs'])

    def test_unpublished_faq_still_shown_in_editor(self):
        # The editor lists all (incl. unpublished); only the agent reads the
        # published-only queryset (Phase 2).
        self._login_admin()
        faq = self._faq(is_published=False)
        response = self.client.get(reverse('tickets:instagram_faq_list'))
        self.assertIn(faq, response.context['faqs'])


class FAQOrgScopingTests(_FAQViewTestBase):
    def test_other_orgs_faq_not_listed(self):
        self._login_admin()
        mine = self._faq(question='Mine')
        theirs = self._faq(organization=self.other_org, question='Theirs')
        response = self.client.get(reverse('tickets:instagram_faq_list'))
        self.assertIn(mine, response.context['faqs'])
        self.assertNotIn(theirs, response.context['faqs'])

    def test_cannot_edit_other_orgs_faq(self):
        self._login_admin()
        theirs = self._faq(organization=self.other_org, question='Theirs')
        self.assertEqual(
            self.client.get(
                reverse('tickets:instagram_faq_edit', args=[theirs.id])
            ).status_code,
            404,
        )

    def test_cannot_delete_other_orgs_faq(self):
        self._login_admin()
        theirs = self._faq(organization=self.other_org, question='Theirs')
        self.assertEqual(
            self.client.post(
                reverse('tickets:instagram_faq_delete', args=[theirs.id])
            ).status_code,
            404,
        )
        theirs.refresh_from_db()
        self.assertIsNone(theirs.deleted_at)


class FAQReorderTests(_FAQViewTestBase):
    def _ordered_questions(self):
        response = self.client.get(reverse('tickets:instagram_faq_list'))
        return [f.question for f in response.context['faqs']]

    def _reorder(self, ids):
        return self.client.post(
            reverse('tickets:instagram_faq_reorder'),
            data=json.dumps({'order': [str(i) for i in ids]}),
            content_type='application/json',
        )

    def test_reorder_persists_new_order(self):
        self._login_admin()
        first = self._faq(question='First', sort_order=0)
        second = self._faq(question='Second', sort_order=1)
        self.assertEqual(self._ordered_questions(), ['First', 'Second'])
        response = self._reorder([second.id, first.id])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'ok': True})
        self.assertEqual(self._ordered_questions(), ['Second', 'First'])

    def test_reorder_ignores_other_orgs_ids(self):
        self._login_admin()
        mine = self._faq(question='Mine', sort_order=0)
        theirs = self._faq(organization=self.other_org, question='Theirs', sort_order=0)
        self._reorder([theirs.id, mine.id])
        theirs.refresh_from_db()
        mine.refresh_from_db()
        self.assertEqual(theirs.sort_order, 0)  # untouched (different org)
        self.assertEqual(mine.sort_order, 1)

    def test_reorder_invalid_payload_400(self):
        self._login_admin()
        response = self.client.post(
            reverse('tickets:instagram_faq_reorder'),
            data='not json', content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)

    def test_reorder_is_post_only(self):
        self._login_admin()
        response = self.client.get(reverse('tickets:instagram_faq_reorder'))
        self.assertEqual(response.status_code, 405)


class FAQInlineAjaxTests(_FAQViewTestBase):
    """Inline (XMLHttpRequest) create/edit/delete answer JSON, not redirects."""

    AJAX = {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}

    def test_create_ajax_returns_json(self):
        self._login_admin()
        response = self.client.post(
            reverse('tickets:instagram_faq_create'),
            {'question': 'Inline Q', 'answer': 'Inline A', 'topic': 'tickets',
             'is_published': 'on', 'sort_order': '0'},
            **self.AJAX,
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['faq']['question'], 'Inline Q')
        faq = OrgFAQ.objects.get(id=body['faq']['id'])
        self.assertEqual(faq.organization, self.org)

    def test_create_ajax_invalid_returns_400(self):
        self._login_admin()
        response = self.client.post(
            reverse('tickets:instagram_faq_create'),
            {'question': '', 'answer': '', 'sort_order': '0'},
            **self.AJAX,
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['ok'])

    def test_edit_ajax_returns_json(self):
        self._login_admin()
        faq = self._faq()
        response = self.client.post(
            reverse('tickets:instagram_faq_edit', args=[faq.id]),
            {'question': 'Edited', 'answer': 'Edited A', 'topic': '',
             'is_published': 'on', 'sort_order': '0'},
            **self.AJAX,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['faq']['question'], 'Edited')

    def test_delete_ajax_returns_json(self):
        self._login_admin()
        faq = self._faq()
        response = self.client.post(
            reverse('tickets:instagram_faq_delete', args=[faq.id]), **self.AJAX,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'ok': True})
        faq.refresh_from_db()
        self.assertIsNotNone(faq.deleted_at)


# ---------------------------------------------------------------------------
# FAQ answer agent (LLM mocked)
# ---------------------------------------------------------------------------


class FAQFormTests(_FAQViewTestBase):
    def test_form_requires_answer(self):
        form = OrgFAQForm(data={
            'question': 'How do I reach you?', 'answer': '',
            'topic': '', 'is_published': 'on', 'sort_order': '0',
        })
        self.assertFalse(form.is_valid())
        self.assertIn('answer', form.errors)


ALLOWED_IG_TOOL_NAMES = {'get_faq', 'list_upcoming_events', 'find_event', 'get_contact_info'}


class IGToolsSafetyTests(TestCase):
    """The customer-safe tool surface: allowlist pin, output scrubbing, visibility.

    This is the main attack surface (the agent speaks to the public over an untrusted
    channel), so these are the priority tests.
    """

    def setUp(self):
        from datetime import date, time, timedelta

        from .models import (
            EVENT_STATUS_CANCELLED, EVENT_STATUS_DRAFT, EVENT_STATUS_LIVE,
            TICKETING_TYPE_DIRECT, TICKETING_TYPE_EXTERNAL, Customer, Event, Venue,
        )

        self.org = Organization.objects.create(name='Tools Org', slug='tools-org')
        self.other_org = Organization.objects.create(name='Other Org', slug='other-tools-org')
        self.venue = Venue.objects.create(organization=self.org, name='The Grand', city='Austin')
        self.today = date.today()

        def make_event(name, *, status, ticketing_type, days=10, **kw):
            return Event.objects.create(
                organization=self.org, name=name, venue=self.venue,
                start_date=self.today + timedelta(days=days),
                start_time=time(21, 0), status=status, ticketing_type=ticketing_type, **kw,
            )

        # Shown: a live direct event and an external event (external status is meaningless).
        self.live_event = make_event(
            'Rooftop Live', status=EVENT_STATUS_LIVE, ticketing_type=TICKETING_TYPE_DIRECT,
            ticket_link='https://tix.example.com/rooftop', capacity=12345,
            summary='A night on the roof.',
        )
        self.external_event = make_event(
            'External Fest', status=EVENT_STATUS_DRAFT, ticketing_type=TICKETING_TYPE_EXTERNAL,
            ticket_link='https://ext.example.com/fest',
        )
        # Hidden: a direct DRAFT, a cancelled, a soft-deleted, and a past event.
        self.draft_event = make_event(
            'Secret Draft Show', status=EVENT_STATUS_DRAFT, ticketing_type=TICKETING_TYPE_DIRECT,
        )
        make_event('Cancelled Gig', status=EVENT_STATUS_CANCELLED, ticketing_type=TICKETING_TYPE_DIRECT)
        self.deleted_event = make_event(
            'Deleted Event', status=EVENT_STATUS_LIVE, ticketing_type=TICKETING_TYPE_DIRECT,
        )
        self.deleted_event.delete()  # soft delete
        self.past_event = make_event(
            'Past External Show', status=EVENT_STATUS_DRAFT,
            ticketing_type=TICKETING_TYPE_EXTERNAL, days=-10,
        )

        # Private data that must NEVER surface through the customer tools.
        Customer.objects.create(
            organization=self.org, email='secret.customer@example.com',
            name='Secret Buyer', lifetime_value=Decimal('98765.43'), rfm_segment='Champions',
        )

    def test_allowlist_is_pinned(self):
        names = {t.name for t in build_ig_tools(self.org)}
        self.assertEqual(names, ALLOWED_IG_TOOL_NAMES)

    def test_tool_outputs_never_leak_private_data(self):
        outputs = [
            _list_upcoming_events(self.org),
            _find_event(self.org, query='Rooftop'),
            _get_faq(self.org),
            _get_contact_info(self.org),
        ]
        blob = "\n".join(outputs)
        for needle in ['$', 'Revenue', 'Profit', 'secret.customer@example.com',
                       'Secret Buyer', '98765', 'Champions', '12345']:
            self.assertNotIn(needle, blob, f"leaked: {needle!r}")

    def test_listing_shows_only_visible_future_events(self):
        out = _list_upcoming_events(self.org, limit=10)
        self.assertIn('Rooftop Live', out)
        self.assertIn('External Fest', out)
        self.assertNotIn('Secret Draft Show', out)
        self.assertNotIn('Cancelled Gig', out)
        self.assertNotIn('Deleted Event', out)
        self.assertNotIn('Past External Show', out)

    def test_find_event_resolves_visible_but_not_draft_or_deleted(self):
        # A visible event resolves with its public details.
        self.assertIn('Rooftop Live', _find_event(self.org, query='Rooftop'))
        # A direct draft and a soft-deleted event must not resolve.
        self.assertIn("couldn't find", _find_event(self.org, query='Secret Draft Show'))
        self.assertIn("couldn't find", _find_event(self.org, query='Deleted Event'))

    def test_find_event_excludes_past_events(self):
        # Regression: find_event previously skipped the future filter that
        # list_upcoming_events applies, so a past event matching by name or city
        # was surfaced as if tickets were available.
        self.assertIn("couldn't find", _find_event(self.org, query='Past External Show'))
        # A city search matches both the past and the future events at this venue;
        # only the future one should come back.
        out = _find_event(self.org, query='Austin')
        self.assertNotIn('Past External Show', out)
        self.assertIn('Rooftop Live', out)

    def test_get_faq_only_published_and_scoped(self):
        OrgFAQ.objects.create(organization=self.org, question='Door time?',
                              answer='PUBLISHED_FAQ_MARKER')
        OrgFAQ.objects.create(organization=self.org, question='Hidden?',
                              answer='HIDDEN_FAQ_MARKER', is_published=False)
        OrgFAQ.objects.create(organization=self.other_org, question='Other?',
                              answer='OTHER_ORG_FAQ_MARKER')
        out = _get_faq(self.org)
        self.assertIn('PUBLISHED_FAQ_MARKER', out)
        self.assertNotIn('HIDDEN_FAQ_MARKER', out)
        self.assertNotIn('OTHER_ORG_FAQ_MARKER', out)


def _ai_message(content='', tool_calls=None, input_tokens=40, output_tokens=20):
    """A fake LangChain AI message carrying usage + tool-call metadata."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls or []
    msg.usage_metadata = {
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'total_tokens': input_tokens + output_tokens,
    }
    return msg


@override_settings(OPENAI_API_KEY='test-key', OPENAI_MODEL='gpt-4o',
                   IG_AGENT_AUTOSEND_MIN_CONFIDENCE=0.8)
class IGAnswerAgentTests(_FAQViewTestBase):
    """InstagramSupportAgentService.answer() with the ReAct agent mocked."""

    @patch('langgraph.prebuilt.create_react_agent')
    @patch('langchain_openai.ChatOpenAI')
    def test_answer_returns_text_usage_tools_and_meters(self, mock_openai, mock_create):
        fake_agent = MagicMock()
        fake_agent.invoke.return_value = {
            'messages': [_ai_message('Doors open at 9pm!', [{'name': 'get_faq'}])],
        }
        mock_create.return_value = fake_agent

        result = InstagramSupportAgentService(self.org).answer(None, 'when do doors open?')

        self.assertIsInstance(result, AnswerResult)
        self.assertEqual(result.text, 'Doors open at 9pm!')
        self.assertEqual(result.tool_calls, ['get_faq'])
        self.assertTrue(result.grounded)

        usage = AITokenUsage.objects.get(organization=self.org)
        self.assertEqual(usage.feature, AITokenUsage.FEATURE_IG_SUPPORT_AGENT)
        self.assertEqual(usage.metadata.get('stage'), 'answer')
        self.assertIsNone(usage.user)
        self.assertEqual(usage.total_tokens, 60)

    @patch('langgraph.prebuilt.create_react_agent')
    @patch('langchain_openai.ChatOpenAI')
    def test_answer_with_no_tool_hit_is_not_grounded(self, mock_openai, mock_create):
        fake_agent = MagicMock()
        fake_agent.invoke.return_value = {'messages': [_ai_message('Sure, maybe!', [])]}
        mock_create.return_value = fake_agent

        result = InstagramSupportAgentService(self.org).answer(None, 'random chit chat')
        self.assertFalse(result.grounded)
        self.assertEqual(result.tool_calls, [])


@override_settings(OPENAI_API_KEY='test-key', OPENAI_MODEL='gpt-4o',
                   IG_AGENT_AUTOSEND_MIN_CONFIDENCE=0.8)
class IGClassifierTests(_FAQViewTestBase):
    """classify_escalation() + decide_autosend() gating."""

    def _mock_llm(self, mock_openai, decision, input_tokens=15, output_tokens=5):
        raw = MagicMock()
        raw.usage_metadata = {
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'total_tokens': input_tokens + output_tokens,
        }
        structured = MagicMock()
        structured.invoke.return_value = {'raw': raw, 'parsed': decision, 'parsing_error': None}
        mock_openai.return_value.with_structured_output.return_value = structured

    @patch('langchain_openai.ChatOpenAI')
    def test_routine_not_escalated_and_meters_classify_stage(self, mock_openai):
        decision = EscalationDecision(should_escalate=False, confidence=0.95,
                                      category='routine', reason='clear faq')
        self._mock_llm(mock_openai, decision)
        out = classify_escalation(self.org, 'what time do doors open?', 'Doors at 9pm!')
        self.assertFalse(out.should_escalate)
        self.assertEqual(out.category, 'routine')

        usage = AITokenUsage.objects.get(organization=self.org)
        self.assertEqual(usage.metadata.get('stage'), 'classify')
        self.assertIsNone(usage.user)
        self.assertEqual(usage.total_tokens, 20)

    @patch('langchain_openai.ChatOpenAI')
    def test_refund_escalates(self, mock_openai):
        decision = EscalationDecision(should_escalate=True, confidence=0.9,
                                      category='refund_dispute', reason='refund')
        self._mock_llm(mock_openai, decision)
        out = classify_escalation(self.org, 'I want a refund', 'A team member will follow up.')
        self.assertTrue(out.should_escalate)
        self.assertEqual(out.category, 'refund_dispute')

    def test_decide_autosend_requires_routine_confident_and_grounded(self):
        grounded = AnswerResult(text='x', tool_calls=['get_faq'])
        ungrounded = AnswerResult(text='x', tool_calls=[])
        routine = EscalationDecision(should_escalate=False, confidence=0.95, category='routine')

        # Happy path: routine + confident + grounded -> auto-send.
        self.assertTrue(decide_autosend(routine, grounded))
        # D14: fluent answer with no tool hit -> queue, even when confident.
        self.assertFalse(decide_autosend(routine, ungrounded))
        # Escalated -> queue.
        self.assertFalse(decide_autosend(
            EscalationDecision(should_escalate=True, confidence=0.99, category='refund_dispute'),
            grounded))
        # Below the confidence threshold -> queue.
        self.assertFalse(decide_autosend(
            EscalationDecision(should_escalate=False, confidence=0.5, category='routine'),
            grounded))

    @patch('langgraph.prebuilt.create_react_agent')
    @patch('langchain_openai.ChatOpenAI')
    def test_management_command_prints_decision(self, mock_openai, mock_create):
        fake_agent = MagicMock()
        fake_agent.invoke.return_value = {
            'messages': [_ai_message('Doors at 9pm!', [{'name': 'get_faq'}])],
        }
        mock_create.return_value = fake_agent
        self._mock_llm(
            mock_openai,
            EscalationDecision(should_escalate=False, confidence=0.95,
                               category='routine', reason='faq'),
        )
        out = StringIO()
        call_command('answer_ig_faq', '--org', self.org.slug,
                     '--question', 'when do doors open?', stdout=out)
        printed = out.getvalue()
        self.assertIn('Doors at 9pm!', printed)
        self.assertIn('AUTO-SEND', printed)


class IGEvaluationGraderTests(TestCase):
    """Pure eval scorers (no LLM / no Langfuse) used by the eval_ig_agent experiment."""

    def test_grade_grounded(self):
        self.assertEqual(grade_grounded({'grounded': True, 'tools': ['get_faq']})[0], True)
        self.assertEqual(grade_grounded({'grounded': False, 'tools': []})[0], False)

    def test_grade_escalation_routine(self):
        self.assertTrue(grade_escalation({'escalated': False, 'category': 'routine'}, 'routine')[0])
        self.assertFalse(grade_escalation({'escalated': True, 'category': 'other'}, 'routine')[0])

    def test_grade_escalation_sensitive_requires_category_match(self):
        out = {'escalated': True, 'category': 'refund_dispute'}
        self.assertTrue(grade_escalation(out, 'refund_dispute')[0])
        self.assertFalse(grade_escalation(out, 'safety')[0])
        self.assertFalse(grade_escalation({'escalated': False, 'category': 'routine'}, 'refund_dispute')[0])

    def test_grade_escalation_not_applicable_returns_none(self):
        self.assertIsNone(grade_escalation({'escalated': True, 'category': 'other'}, ''))

    def test_grade_tool(self):
        self.assertTrue(grade_tool({'tools': ['get_faq', 'find_event']}, 'get_faq')[0])
        self.assertFalse(grade_tool({'tools': ['get_contact_info']}, 'get_faq')[0])
        self.assertIsNone(grade_tool({'tools': []}, ''))

    @patch('langchain_openai.ChatOpenAI')
    def test_judge_passes_a_refusal_that_echoes_the_term(self, mock_openai):
        # The failure case we saw live: a refusal that names "lifetime value" is NOT a leak.
        structured = MagicMock()
        structured.invoke.return_value = DisclosureVerdict(disclosed=False, reason='refusal only')
        mock_openai.return_value.with_structured_output.return_value = structured

        passed, _ = judge_private_disclosure(
            "I can't access or share any internal data like customer emails or lifetime value.",
            ['lifetime value', '@'],
        )
        self.assertTrue(passed)

    @patch('langchain_openai.ChatOpenAI')
    def test_judge_flags_a_real_disclosure(self, mock_openai):
        structured = MagicMock()
        structured.invoke.return_value = DisclosureVerdict(disclosed=True, reason='revealed email + LTV')
        mock_openai.return_value.with_structured_output.return_value = structured

        passed, reason = judge_private_disclosure("Top customer: jane@example.com, LTV $980.", ['@'])
        self.assertFalse(passed)
        self.assertIn('email', reason)

    def test_bundled_corpus_loads_and_is_well_formed(self):
        from pathlib import Path
        from django.conf import settings

        cases = load_cases(Path(settings.BASE_DIR) / 'evals' / 'ig_support_agent' / 'cases.jsonl')
        self.assertGreater(len(cases), 0)
        allowed_tools = {'', 'get_faq', 'list_upcoming_events', 'find_event', 'get_contact_info'}
        allowed_cats = {'', 'routine', 'refund_dispute', 'complaint', 'partnership',
                        'guest_list', 'safety', 'other'}
        for case in cases:
            self.assertIn('input', case)
            self.assertIn(case.get('expected_tool', ''), allowed_tools)
            self.assertIn(case.get('expected_category', ''), allowed_cats)


# ---------------------------------------------------------------------------
# Phase 3 — transport + inbound + orchestration + webhook
# ---------------------------------------------------------------------------

from .services.instagram import (  # noqa: E402
    InstagramAgentError, NormalizedInbound, SendResult, StubSender,
    get_sender, normalize_meta_payload, verify_meta_signature,
)


class IGNormalizeTests(TestCase):
    """normalize_meta_payload: keep real text DMs, skip the rest."""

    def _payload(self, messaging):
        return {'object': 'instagram',
                'entry': [{'id': 'acct-1', 'time': 1, 'messaging': messaging}]}

    def test_extracts_text_message(self):
        out = normalize_meta_payload(self._payload([
            {'sender': {'id': 'cust-1'}, 'recipient': {'id': 'acct-1'},
             'timestamp': 99, 'message': {'mid': 'm1', 'text': 'is it sold out?'}},
        ]))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].ig_account_id, 'acct-1')
        self.assertEqual(out[0].sender_id, 'cust-1')
        self.assertEqual(out[0].text, 'is it sold out?')
        self.assertEqual(out[0].provider_message_id, 'm1')
        self.assertEqual(out[0].timestamp, 99)

    def test_keeps_story_reply_with_text(self):
        # Story replies arrive with text PLUS an attachment / reply_to.story.
        out = normalize_meta_payload(self._payload([
            {'sender': {'id': 'cust-1'}, 'recipient': {'id': 'acct-1'},
             'message': {'mid': 'm2', 'text': 'love this lineup!',
                         'reply_to': {'story': {'id': 's1'}},
                         'attachments': [{'type': 'story_mention'}]}},
        ]))
        self.assertEqual([n.text for n in out], ['love this lineup!'])

    def test_skips_echo_reaction_receipt_and_attachment_only(self):
        out = normalize_meta_payload(self._payload([
            {'sender': {'id': 'acct-1'}, 'message': {'mid': 'e1', 'text': 'hi', 'is_echo': True}},
            {'sender': {'id': 'cust-1'}, 'reaction': {'emoji': '❤️'}},
            {'sender': {'id': 'cust-1'}, 'read': {'mid': 'r1'}},
            {'sender': {'id': 'cust-1'}, 'message': {'mid': 'a1', 'attachments': [{'type': 'image'}]}},
        ]))
        self.assertEqual(out, [])

    def test_multiple_messaging_entries(self):
        out = normalize_meta_payload({'entry': [
            {'id': 'acct-1', 'messaging': [
                {'sender': {'id': 'c1'}, 'message': {'mid': 'm1', 'text': 'one'}},
                {'sender': {'id': 'c2'}, 'message': {'mid': 'm2', 'text': 'two'}},
            ]},
        ]})
        self.assertEqual([n.text for n in out], ['one', 'two'])

    def test_non_dict_body_is_empty(self):
        self.assertEqual(normalize_meta_payload(None), [])
        self.assertEqual(normalize_meta_payload('nope'), [])


@override_settings(FACEBOOK_APP_SECRET='top-secret', E2E_TEST_MODE=False)
class IGVerifySignatureTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _req(self, body: bytes, sig_hex=None):
        headers = {}
        if sig_hex is not None:
            headers['HTTP_X_HUB_SIGNATURE_256'] = f'sha256={sig_hex}'
        return self.factory.post('/webhooks/instagram/', data=body,
                                 content_type='application/json', **headers)

    @override_settings(INSTAGRAM_VALIDATE_WEBHOOKS=True)
    def test_valid_signature(self):
        body = b'{"hello":"world"}'
        good = hmac.new(b'top-secret', body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_meta_signature(self._req(body, good)))

    @override_settings(INSTAGRAM_VALIDATE_WEBHOOKS=True)
    def test_invalid_signature(self):
        self.assertFalse(verify_meta_signature(self._req(b'{"a":1}', 'deadbeef')))

    @override_settings(INSTAGRAM_VALIDATE_WEBHOOKS=True)
    def test_missing_header(self):
        self.assertFalse(verify_meta_signature(self._req(b'{"a":1}', None)))

    @override_settings(INSTAGRAM_VALIDATE_WEBHOOKS=False)
    def test_bypass_flag(self):
        self.assertTrue(verify_meta_signature(self._req(b'{"a":1}', 'deadbeef')))

    @override_settings(INSTAGRAM_VALIDATE_WEBHOOKS=True, E2E_TEST_MODE=True)
    def test_e2e_mode_bypass(self):
        self.assertTrue(verify_meta_signature(self._req(b'{"a":1}', 'deadbeef')))


class IGGetSenderTests(TestCase):
    @override_settings(INSTAGRAM_SENDER_BACKEND='stub')
    def test_stub_backend(self):
        sender = get_sender(MagicMock())
        self.assertIsInstance(sender, StubSender)
        self.assertTrue(sender.send_text('cust-1', 'hi').ok)

    @override_settings(INSTAGRAM_SENDER_BACKEND='graph')
    def test_graph_backend_fails_loud_no_stub_fallback(self):
        sender = get_sender(MagicMock())
        self.assertNotIsInstance(sender, StubSender)
        result = sender.send_text('cust-1', 'hi')
        self.assertFalse(result.ok)
        self.assertTrue(result.error)


@override_settings(INSTAGRAM_WEBHOOK_VERIFY_TOKEN='verify-me',
                   FACEBOOK_APP_SECRET='top-secret', E2E_TEST_MODE=False)
class IGWebhookViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = reverse('tickets:instagram_webhook')
        self.org = Organization.objects.create(
            name='IG Org', slug='ig-webhook-org',
            instagram_business_account_id='acct-1',
            instagram_support_agent_enabled=True,
        )

    # --- GET verification handshake ---
    def test_get_verify_token_match_returns_challenge(self):
        resp = self.client.get(self.url, {
            'hub.mode': 'subscribe', 'hub.verify_token': 'verify-me',
            'hub.challenge': '12345',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b'12345')

    def test_get_verify_token_mismatch_403(self):
        resp = self.client.get(self.url, {
            'hub.mode': 'subscribe', 'hub.verify_token': 'wrong', 'hub.challenge': '1',
        })
        self.assertEqual(resp.status_code, 403)

    # --- POST signature + routing ---
    def _signed_post(self, body_dict):
        body = json.dumps(body_dict).encode('utf-8')
        sig = hmac.new(b'top-secret', body, hashlib.sha256).hexdigest()
        return self.client.post(
            self.url, data=body, content_type='application/json',
            HTTP_X_HUB_SIGNATURE_256=f'sha256={sig}',
        )

    def _dm_body(self, account_id='acct-1', mid='m1'):
        return {'entry': [{'id': account_id, 'messaging': [
            {'sender': {'id': 'cust-1'}, 'recipient': {'id': account_id},
             'message': {'mid': mid, 'text': 'hi'}},
        ]}]}

    @override_settings(INSTAGRAM_VALIDATE_WEBHOOKS=True)
    def test_bad_signature_403_and_no_enqueue(self):
        with patch('tickets.tasks.process_instagram_inbound_task') as task:
            resp = self.client.post(
                self.url, data=json.dumps(self._dm_body()).encode(),
                content_type='application/json',
                HTTP_X_HUB_SIGNATURE_256='sha256=bad',
            )
        self.assertEqual(resp.status_code, 403)
        task.delay.assert_not_called()

    @override_settings(INSTAGRAM_VALIDATE_WEBHOOKS=True)
    def test_valid_signature_enqueues(self):
        with patch('tickets.tasks.process_instagram_inbound_task') as task:
            resp = self._signed_post(self._dm_body())
        self.assertEqual(resp.status_code, 200)
        task.delay.assert_called_once()
        args = task.delay.call_args.args
        self.assertEqual(args[0], str(self.org.id))
        self.assertEqual(args[1]['text'], 'hi')

    @override_settings(INSTAGRAM_VALIDATE_WEBHOOKS=True)
    def test_unknown_account_200_no_enqueue(self):
        with patch('tickets.tasks.process_instagram_inbound_task') as task:
            resp = self._signed_post(self._dm_body(account_id='nope'))
        self.assertEqual(resp.status_code, 200)
        task.delay.assert_not_called()

    @override_settings(INSTAGRAM_VALIDATE_WEBHOOKS=True)
    def test_malformed_json_200_no_500(self):
        body = b'{not json'
        sig = hmac.new(b'top-secret', body, hashlib.sha256).hexdigest()
        resp = self.client.post(self.url, data=body, content_type='application/json',
                                HTTP_X_HUB_SIGNATURE_256=f'sha256={sig}')
        self.assertEqual(resp.status_code, 200)


@override_settings(OPENAI_API_KEY='test-key', OPENAI_MODEL='gpt-4o',
                   IG_AGENT_AUTOSEND_MIN_CONFIDENCE=0.8,
                   INSTAGRAM_SENDER_BACKEND='stub', IG_AGENT_DAILY_ANSWER_CAP=200)
class IGOrchestrationTaskTests(TestCase):
    """process_instagram_inbound_task with the LLM pipeline mocked."""

    def setUp(self):
        self.org = Organization.objects.create(
            name='Orch Org', slug='orch-org',
            instagram_business_account_id='acct-1',
            instagram_support_agent_enabled=True,
        )

    def _normalized(self, text='when do doors open?', mid='m1', sender='cust-1'):
        return {'ig_account_id': 'acct-1', 'sender_id': sender, 'text': text,
                'provider_message_id': mid, 'timestamp': 0}

    def _run(self, **kw):
        from tickets.tasks import process_instagram_inbound_task
        process_instagram_inbound_task.apply(args=[str(self.org.id), self._normalized(**kw)])

    def _patch_pipeline(self, *, text='Doors at 9pm!', tools=('get_faq',),
                        escalate=False, category='routine', confidence=0.95):
        svc = patch('tickets.services.instagram.InstagramSupportAgentService')
        cls = svc.start()
        self.addCleanup(svc.stop)
        cls.return_value.answer.return_value = AnswerResult(text=text, tool_calls=list(tools))
        clf = patch('tickets.services.instagram.classify_escalation',
                    return_value=EscalationDecision(should_escalate=escalate,
                                                    confidence=confidence, category=category,
                                                    reason='r'))
        clf.start()
        self.addCleanup(clf.stop)
        return cls

    def test_routine_grounded_auto_sends(self):
        self._patch_pipeline()
        self._run()
        conv = InstagramConversation.objects.get(organization=self.org, ig_user_id='cust-1')
        self.assertEqual(conv.status, InstagramConversation.STATUS_OPEN)
        out = InstagramMessage.objects.get(conversation=conv,
                                           direction=InstagramMessage.DIRECTION_OUTBOUND)
        self.assertEqual(out.status, InstagramMessage.STATUS_AUTO_SENT)
        self.assertTrue(out.provider_message_id)

    def test_sensitive_queues_and_flags_conversation(self):
        self._patch_pipeline(escalate=True, category='refund_dispute')
        self._run(text='I need a refund')
        conv = InstagramConversation.objects.get(organization=self.org, ig_user_id='cust-1')
        self.assertEqual(conv.status, InstagramConversation.STATUS_AWAITING_HUMAN)
        out = InstagramMessage.objects.get(conversation=conv,
                                           direction=InstagramMessage.DIRECTION_OUTBOUND)
        self.assertEqual(out.status, InstagramMessage.STATUS_PENDING_REVIEW)
        self.assertEqual(out.escalation_category, 'refund_dispute')

    def test_ungrounded_answer_queued(self):
        self._patch_pipeline(tools=())  # no tool hit -> not grounded -> D14 queue
        self._run()
        out = InstagramMessage.objects.get(conversation__organization=self.org,
                                           direction=InstagramMessage.DIRECTION_OUTBOUND)
        self.assertEqual(out.status, InstagramMessage.STATUS_PENDING_REVIEW)

    def test_agent_disabled_no_op(self):
        self.org.instagram_support_agent_enabled = False
        self.org.save(update_fields=['instagram_support_agent_enabled'])
        self._patch_pipeline()
        self._run()
        self.assertFalse(InstagramMessage.objects.filter(organization=self.org).exists())

    def test_unknown_org_no_op(self):
        from tickets.tasks import process_instagram_inbound_task
        import uuid
        # Should not raise.
        process_instagram_inbound_task.apply(args=[str(uuid.uuid4()), self._normalized()])

    def test_duplicate_delivery_one_inbound_one_send(self):
        self._patch_pipeline()
        self._run(mid='dup')
        self._run(mid='dup')
        conv = InstagramConversation.objects.get(organization=self.org, ig_user_id='cust-1')
        self.assertEqual(InstagramMessage.objects.filter(
            conversation=conv, direction=InstagramMessage.DIRECTION_INBOUND).count(), 1)
        self.assertEqual(InstagramMessage.objects.filter(
            conversation=conv, direction=InstagramMessage.DIRECTION_OUTBOUND).count(), 1)

    def test_resumes_when_inbound_committed_but_no_reply(self):
        # Simulate a prior run that committed the inbound row then died before replying.
        conv = InstagramConversation.objects.create(organization=self.org, ig_user_id='cust-1')
        InstagramMessage.objects.create(
            conversation=conv, direction=InstagramMessage.DIRECTION_INBOUND,
            author=InstagramMessage.AUTHOR_CUSTOMER, content='when do doors open?',
            provider_message_id='resume-1', status=InstagramMessage.STATUS_RECEIVED,
        )
        self._patch_pipeline()
        self._run(mid='resume-1')
        # No duplicate inbound; the retry produced exactly one reply.
        self.assertEqual(InstagramMessage.objects.filter(
            conversation=conv, direction=InstagramMessage.DIRECTION_INBOUND).count(), 1)
        out = InstagramMessage.objects.get(conversation=conv,
                                           direction=InstagramMessage.DIRECTION_OUTBOUND)
        self.assertEqual(out.status, InstagramMessage.STATUS_AUTO_SENT)

    def test_transient_pipeline_error_leaves_no_reply(self):
        svc = patch('tickets.services.instagram.InstagramSupportAgentService')
        cls = svc.start()
        self.addCleanup(svc.stop)
        cls.return_value.answer.side_effect = InstagramAgentError('LLM down')
        try:
            self._run()
        except Exception:
            pass  # eager retry eventually raises MaxRetriesExceeded — fine
        conv = InstagramConversation.objects.get(organization=self.org, ig_user_id='cust-1')
        self.assertEqual(InstagramMessage.objects.filter(
            conversation=conv, direction=InstagramMessage.DIRECTION_OUTBOUND).count(), 0)
        # Inbound was committed before the pipeline ran (dedup anchor is durable).
        self.assertTrue(InstagramMessage.objects.filter(
            conversation=conv, direction=InstagramMessage.DIRECTION_INBOUND).exists())

    @override_settings(IG_AGENT_DAILY_ANSWER_CAP=1)
    def test_daily_cap_reached_queues(self):
        conv = InstagramConversation.objects.create(organization=self.org, ig_user_id='cust-1')
        InstagramMessage.objects.create(
            conversation=conv, direction=InstagramMessage.DIRECTION_OUTBOUND,
            author=InstagramMessage.AUTHOR_AGENT, content='earlier',
            status=InstagramMessage.STATUS_AUTO_SENT, provider_message_id='prev',
        )
        self._patch_pipeline()
        self._run(mid='capped')
        out = InstagramMessage.objects.filter(
            conversation=conv, direction=InstagramMessage.DIRECTION_OUTBOUND,
        ).exclude(provider_message_id='prev').get()
        self.assertEqual(out.status, InstagramMessage.STATUS_PENDING_REVIEW)

    @override_settings(IG_AGENT_DAILY_ANSWER_CAP=2)
    def test_just_under_daily_cap_auto_sends(self):
        conv = InstagramConversation.objects.create(organization=self.org, ig_user_id='cust-1')
        InstagramMessage.objects.create(
            conversation=conv, direction=InstagramMessage.DIRECTION_OUTBOUND,
            author=InstagramMessage.AUTHOR_AGENT, content='earlier',
            status=InstagramMessage.STATUS_AUTO_SENT, provider_message_id='prev',
        )
        self._patch_pipeline()
        self._run(mid='under')
        out = InstagramMessage.objects.filter(
            conversation=conv, direction=InstagramMessage.DIRECTION_OUTBOUND,
        ).exclude(provider_message_id='prev').get()
        self.assertEqual(out.status, InstagramMessage.STATUS_AUTO_SENT)

    def test_latest_inbound_guard_queues_stale_fragment(self):
        conv = InstagramConversation.objects.create(organization=self.org, ig_user_id='cust-1')
        newer = InstagramMessage.objects.create(
            conversation=conv, direction=InstagramMessage.DIRECTION_INBOUND,
            author=InstagramMessage.AUTHOR_CUSTOMER, content='later fragment',
            provider_message_id='newer', status=InstagramMessage.STATUS_RECEIVED,
        )
        InstagramMessage.objects.filter(pk=newer.pk).update(
            created_at=timezone.now() + timezone.timedelta(minutes=5))
        self._patch_pipeline()
        self._run(mid='older')  # task's inbound is created "now", older than `newer`
        out = InstagramMessage.objects.get(conversation=conv,
                                           direction=InstagramMessage.DIRECTION_OUTBOUND)
        self.assertEqual(out.status, InstagramMessage.STATUS_PENDING_REVIEW)

    def test_send_failure_marks_failed_not_awaiting(self):
        self._patch_pipeline()
        failing = MagicMock()
        failing.send_text.return_value = SendResult(ok=False, error='boom')
        with patch('tickets.services.instagram.get_sender', return_value=failing):
            self._run()
        conv = InstagramConversation.objects.get(organization=self.org, ig_user_id='cust-1')
        self.assertEqual(conv.status, InstagramConversation.STATUS_OPEN)
        out = InstagramMessage.objects.get(conversation=conv,
                                           direction=InstagramMessage.DIRECTION_OUTBOUND)
        self.assertEqual(out.status, InstagramMessage.STATUS_FAILED)


@override_settings(OPENAI_API_KEY='test-key', OPENAI_MODEL='gpt-4o',
                   INSTAGRAM_SENDER_BACKEND='stub', IG_AGENT_AUTOSEND_MIN_CONFIDENCE=0.8)
class IGSimulateCommandTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name='Sim Org', slug='sim-org',
            instagram_business_account_id='acct-sim',
            instagram_support_agent_enabled=True,
        )

    def _patch(self, escalate=False, category='routine', tools=('get_faq',)):
        svc = patch('tickets.services.instagram.InstagramSupportAgentService')
        cls = svc.start(); self.addCleanup(svc.stop)
        cls.return_value.answer.return_value = AnswerResult(text='Doors at 9pm!', tool_calls=list(tools))
        clf = patch('tickets.services.instagram.classify_escalation',
                    return_value=EscalationDecision(should_escalate=escalate, confidence=0.95,
                                                    category=category, reason='r'))
        clf.start(); self.addCleanup(clf.stop)

    def test_routine_auto_sends(self):
        self._patch()
        out = StringIO()
        call_command('simulate_instagram_dm', '--org', 'sim-org',
                     '--text', 'when do doors open?', stdout=out)
        self.assertIn('auto_sent', out.getvalue())

    def test_refund_pending_review(self):
        self._patch(escalate=True, category='refund_dispute')
        out = StringIO()
        call_command('simulate_instagram_dm', '--org', 'sim-org',
                     '--text', 'I need a refund', stdout=out)
        self.assertIn('pending_review', out.getvalue())
