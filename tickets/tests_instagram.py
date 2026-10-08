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
    _find_event, _get_contact_info, _get_faq, _list_past_events, _list_upcoming_events,
)
from .services.instagram.evaluation import (
    AnswerQualityVerdict, DisclosureVerdict, grade_answer_quality,
    grade_escalation, grade_grounded, grade_tool, judge_private_disclosure,
    load_cases,
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
        self.org = Organization.objects.create(
            name='FAQ View Org', slug='faq-view-org', instagram_feature_enabled=True,
        )
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


ALLOWED_IG_TOOL_NAMES = {'get_faq', 'list_upcoming_events', 'list_past_events',
                         'find_event', 'get_contact_info'}


class IGToolsSafetyTests(TestCase):
    """The customer-safe tool surface: allowlist pin, output scrubbing, visibility.

    This is the main attack surface (the agent speaks to the public over an untrusted
    channel), so these are the priority tests.
    """

    def setUp(self):
        from datetime import date, time, timedelta

        from .models import (
            EVENT_STATUS_CANCELLED, EVENT_STATUS_DRAFT, EVENT_STATUS_ENDED,
            EVENT_STATUS_LIVE, TICKETING_TYPE_DIRECT, TICKETING_TYPE_EXTERNAL,
            Customer, Event, Venue,
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
        # A direct event that already ran ends up ENDED (not LIVE) — it must still show in
        # past listings. A past direct DRAFT (never published) must stay hidden.
        self.past_direct_ended = make_event(
            'Past Direct Ended Show', status=EVENT_STATUS_ENDED,
            ticketing_type=TICKETING_TYPE_DIRECT, days=-5,
        )
        self.past_direct_draft = make_event(
            'Past Direct Draft Show', status=EVENT_STATUS_DRAFT,
            ticketing_type=TICKETING_TYPE_DIRECT, days=-7,
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
            _list_past_events(self.org),
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

    def test_list_past_events_shows_only_visible_past_events(self):
        out = _list_past_events(self.org, limit=10)
        # Visible past events: an external show AND a direct show that has ENDED.
        self.assertIn('Past External Show', out)
        self.assertIn('Past Direct Ended Show', out)
        # Future events, a past direct DRAFT, and other hidden events are not listed.
        self.assertNotIn('Past Direct Draft Show', out)
        self.assertNotIn('Rooftop Live', out)
        self.assertNotIn('External Fest', out)
        self.assertNotIn('Secret Draft Show', out)
        self.assertNotIn('Cancelled Gig', out)
        self.assertNotIn('Deleted Event', out)

    def test_list_past_events_most_recent_first(self):
        # The ENDED direct show (-5 days) is more recent than the external one (-10).
        out = _list_past_events(self.org, limit=10)
        self.assertLess(out.index('Past Direct Ended Show'), out.index('Past External Show'))

    def test_list_past_events_empty_when_none(self):
        # Remove every visible past event (the hidden draft doesn't count).
        self.past_event.hard_delete()
        self.past_direct_ended.hard_delete()
        self.assertIn('no past events', _list_past_events(self.org).lower())

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

    @patch('langgraph.prebuilt.create_react_agent')
    @patch('langchain_openai.ChatOpenAI')
    def test_needs_human_sentinel_sets_flag_and_is_stripped(self, mock_openai, mock_create):
        from tickets.services.instagram.prompts import NEEDS_HUMAN_SENTINEL

        fake_agent = MagicMock()
        fake_agent.invoke.return_value = {
            'messages': [_ai_message(
                "I'm not sure when our last event was — a team member will follow up!\n"
                + NEEDS_HUMAN_SENTINEL,
                [{'name': 'list_upcoming_events'}],
            )],
        }
        mock_create.return_value = fake_agent

        result = InstagramSupportAgentService(self.org).answer(None, 'when was your last event?')
        self.assertTrue(result.needs_human)
        # The marker must never reach the customer-facing text.
        self.assertNotIn(NEEDS_HUMAN_SENTINEL, result.text)
        self.assertTrue(result.text.endswith('follow up!'))

    @patch('langgraph.prebuilt.create_react_agent')
    @patch('langchain_openai.ChatOpenAI')
    def test_no_sentinel_leaves_needs_human_false(self, mock_openai, mock_create):
        fake_agent = MagicMock()
        fake_agent.invoke.return_value = {
            'messages': [_ai_message('Doors open at 9pm!', [{'name': 'get_faq'}])],
        }
        mock_create.return_value = fake_agent

        result = InstagramSupportAgentService(self.org).answer(None, 'when do doors open?')
        self.assertFalse(result.needs_human)


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

    def test_grade_answer_quality_skips_when_no_reference(self):
        # No expected_answer on the case → the quality judge doesn't apply (no LLM call).
        self.assertIsNone(grade_answer_quality({'text': 'Doors at 8pm!'}, ''))
        self.assertIsNone(grade_answer_quality({'text': 'Doors at 8pm!'}, '   '))

    @patch('langchain_openai.ChatOpenAI')
    def test_grade_answer_quality_passes_a_faithful_reply(self, mock_openai):
        structured = MagicMock()
        structured.invoke.return_value = AnswerQualityVerdict(
            meets_bar=True, reason='faithful and on-brand')
        mock_openai.return_value.with_structured_output.return_value = structured

        passed, reason = grade_answer_quality(
            {'text': 'Tickets are online via the link in our bio.'},
            'Tickets are sold online through the link in our bio.',
        )
        self.assertTrue(passed)
        self.assertIn('faithful', reason)

    @patch('langchain_openai.ChatOpenAI')
    def test_grade_answer_quality_flags_a_hallucinated_reply(self, mock_openai):
        structured = MagicMock()
        structured.invoke.return_value = AnswerQualityVerdict(
            meets_bar=False, reason='invented a $25 price not in the reference')
        mock_openai.return_value.with_structured_output.return_value = structured

        passed, reason = grade_answer_quality(
            {'text': 'Tickets are $25 at the door.'},
            'Tickets are sold online through the link in our bio.',
        )
        self.assertFalse(passed)
        self.assertIn('invented', reason)

    def test_bundled_corpus_loads_and_is_well_formed(self):
        from pathlib import Path
        from django.conf import settings

        cases = load_cases(Path(settings.BASE_DIR) / 'evals' / 'ig_support_agent' / 'cases.jsonl')
        self.assertGreater(len(cases), 0)
        allowed_tools = {'', 'get_faq', 'list_upcoming_events', 'list_past_events',
                         'find_event', 'get_contact_info'}
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
                        escalate=False, category='routine', confidence=0.95,
                        needs_human=False):
        svc = patch('tickets.services.instagram.InstagramSupportAgentService')
        cls = svc.start()
        self.addCleanup(svc.stop)
        cls.return_value.answer.return_value = AnswerResult(
            text=text, tool_calls=list(tools), needs_human=needs_human)
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

    def test_sensitive_escalates_and_flags_conversation_without_draft(self):
        self._patch_pipeline(escalate=True, category='refund_dispute')
        self._run(text='I need a refund')
        conv = InstagramConversation.objects.get(organization=self.org, ig_user_id='cust-1')
        self.assertEqual(conv.status, InstagramConversation.STATUS_AWAITING_HUMAN)
        # A true escalation produces no agent draft — the handoff reply isn't useful.
        self.assertFalse(InstagramMessage.objects.filter(
            conversation=conv, direction=InstagramMessage.DIRECTION_OUTBOUND,
            author=InstagramMessage.AUTHOR_AGENT).exists())

    def test_answer_agent_deferral_escalates_even_when_classifier_routine(self):
        # The agent promised a human follow-up (needs_human) but the classifier rated it
        # routine + grounded + confident. The deferral must still queue a human: flip to
        # awaiting_human, save no agent draft, and notify once.
        self._patch_pipeline(
            text="I don't have access to past event details — a team member will follow up!",
            tools=('list_upcoming_events',), escalate=False, category='routine',
            confidence=0.9, needs_human=True,
        )
        with patch('tickets.tasks.notify_instagram_escalation_task.delay') as delay:
            with self.captureOnCommitCallbacks(execute=True):
                self._run(text='when was your last event?')
        conv = InstagramConversation.objects.get(organization=self.org, ig_user_id='cust-1')
        self.assertEqual(conv.status, InstagramConversation.STATUS_AWAITING_HUMAN)
        # Treated as a true escalation: the non-answer handoff draft is not saved.
        self.assertFalse(InstagramMessage.objects.filter(
            conversation=conv, direction=InstagramMessage.DIRECTION_OUTBOUND,
            author=InstagramMessage.AUTHOR_AGENT).exists())
        delay.assert_called_once()

    def test_agent_stays_out_during_human_handling(self):
        # A human has taken the thread over (human_handling). A new customer message must
        # be recorded but the agent must NOT draft or auto-send over the human.
        conv = InstagramConversation.objects.create(
            organization=self.org, ig_user_id='cust-1',
            status=InstagramConversation.STATUS_HUMAN_HANDLING)
        self._patch_pipeline()  # would auto-send if the gate let it through
        self._run(text='one more thing…')
        conv.refresh_from_db()
        self.assertEqual(conv.status, InstagramConversation.STATUS_HUMAN_HANDLING)
        # Inbound recorded so the human sees it...
        self.assertTrue(InstagramMessage.objects.filter(
            conversation=conv, direction=InstagramMessage.DIRECTION_INBOUND,
            content='one more thing…').exists())
        # ...but the agent produced no outbound at all.
        self.assertFalse(InstagramMessage.objects.filter(
            conversation=conv, direction=InstagramMessage.DIRECTION_OUTBOUND,
            author=InstagramMessage.AUTHOR_AGENT).exists())

    def test_resolved_conversation_lets_agent_reply(self):
        # After a hand-back (status resolved) the agent resumes on the next message.
        InstagramConversation.objects.create(
            organization=self.org, ig_user_id='cust-1',
            status=InstagramConversation.STATUS_RESOLVED)
        self._patch_pipeline()
        self._run(text='when do doors open?')
        out = InstagramMessage.objects.get(
            conversation__organization=self.org,
            direction=InstagramMessage.DIRECTION_OUTBOUND,
            author=InstagramMessage.AUTHOR_AGENT)
        self.assertEqual(out.status, InstagramMessage.STATUS_AUTO_SENT)

    def test_ungrounded_answer_queued(self):
        self._patch_pipeline(tools=())  # no tool hit -> not grounded -> D14 queue
        self._run()
        out = InstagramMessage.objects.get(conversation__organization=self.org,
                                           direction=InstagramMessage.DIRECTION_OUTBOUND,
                                           author=InstagramMessage.AUTHOR_AGENT)
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
        out = InstagramMessage.objects.get(
            conversation=conv, direction=InstagramMessage.DIRECTION_OUTBOUND,
            status=InstagramMessage.STATUS_PENDING_REVIEW,
        )
        self.assertEqual(out.author, InstagramMessage.AUTHOR_AGENT)

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
                                           direction=InstagramMessage.DIRECTION_OUTBOUND,
                                           author=InstagramMessage.AUTHOR_AGENT)
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

    def test_refund_escalates_without_draft(self):
        self._patch(escalate=True, category='refund_dispute')
        out = StringIO()
        call_command('simulate_instagram_dm', '--org', 'sim-org',
                     '--text', 'I need a refund', stdout=out)
        output = out.getvalue()
        self.assertIn('awaiting_human', output)
        self.assertIn('escalated to a human', output)


# ---------------------------------------------------------------------------
# Phase 4 — human-review inbox + escalation notifications
# ---------------------------------------------------------------------------

from django.core import mail  # noqa: E402


@override_settings(INSTAGRAM_SENDER_BACKEND='stub')
class _InboxViewTestBase(_FAQViewTestBase):
    """Reuses the FAQ base (admin + host + other_org) with IG thread helpers."""

    def _conversation(self, organization=None, ig_user_id='cust-1',
                      status=InstagramConversation.STATUS_AWAITING_HUMAN, **kwargs):
        return InstagramConversation.objects.create(
            organization=organization or self.org,
            ig_user_id=ig_user_id, status=status, **kwargs,
        )

    def _draft(self, conv, content='Doors at 9pm!', category='routine'):
        # save() copies organization from the conversation (tenancy invariant).
        return InstagramMessage.objects.create(
            conversation=conv, direction=InstagramMessage.DIRECTION_OUTBOUND,
            author=InstagramMessage.AUTHOR_AGENT, content=content,
            status=InstagramMessage.STATUS_PENDING_REVIEW, escalation_category=category,
        )


class InstagramFeatureFlagTests(_InboxViewTestBase):
    def test_ux_404s_when_feature_off(self):
        self.org.instagram_feature_enabled = False
        self.org.save(update_fields=['instagram_feature_enabled'])
        self._login_admin()
        conv = self._conversation()
        faq = self._faq()
        for url in (
            reverse('tickets:instagram_inbox'),
            reverse('tickets:instagram_conversation_detail', args=[conv.id]),
            reverse('tickets:instagram_faq_list'),
            reverse('tickets:instagram_faq_edit', args=[faq.id]),
        ):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        self.assertEqual(
            self.client.post(
                reverse('tickets:instagram_agent_settings'),
                {'escalation_ack_text': 'x'}).status_code,
            404,
        )

    def test_ux_visible_when_feature_on(self):
        self._login_admin()  # base sets instagram_feature_enabled=True
        self.assertEqual(
            self.client.get(reverse('tickets:instagram_inbox')).status_code, 200)
        self.assertEqual(
            self.client.get(reverse('tickets:instagram_faq_list')).status_code, 200)


class InstagramRegistryGateTests(TestCase):
    def test_card_hidden_when_feature_off(self):
        from tickets.integrations.registry import integration_statuses
        org = Organization.objects.create(name='Reg Off', slug='reg-off')
        keys = {e['key'] for e in integration_statuses(org)}
        self.assertNotIn('instagram', keys)

    def test_card_shown_when_feature_on(self):
        from tickets.integrations.registry import integration_statuses
        org = Organization.objects.create(
            name='Reg On', slug='reg-on', instagram_feature_enabled=True)
        keys = {e['key'] for e in integration_statuses(org)}
        self.assertIn('instagram', keys)


class InboxAccessControlTests(_InboxViewTestBase):
    def test_non_admin_forbidden(self):
        self._login_host()
        conv = self._conversation()
        for url in (
            reverse('tickets:instagram_inbox'),
            reverse('tickets:instagram_conversation_detail', args=[conv.id]),
        ):
            self.assertEqual(self.client.get(url).status_code, 403, url)

    def test_admin_can_open_inbox(self):
        self._login_admin()
        self.assertEqual(
            self.client.get(reverse('tickets:instagram_inbox')).status_code, 200,
        )


class InboxOrgScopingTests(_InboxViewTestBase):
    def test_only_own_orgs_conversations_listed(self):
        self._login_admin()
        mine = self._conversation(ig_user_id='mine')
        theirs = self._conversation(organization=self.other_org, ig_user_id='theirs')
        convs = list(self.client.get(
            reverse('tickets:instagram_inbox')).context['conversations'])
        self.assertIn(mine, convs)
        self.assertNotIn(theirs, convs)

    def test_cannot_open_other_orgs_conversation(self):
        self._login_admin()
        theirs = self._conversation(organization=self.other_org, ig_user_id='theirs')
        self.assertEqual(
            self.client.get(
                reverse('tickets:instagram_conversation_detail', args=[theirs.id])
            ).status_code,
            404,
        )


class ConversationDetailThreadTests(_InboxViewTestBase):
    def test_pending_draft_excluded_from_inline_thread(self):
        # The un-sent draft belongs in the editor, not the sent-message timeline.
        self._login_admin()
        conv = self._conversation()
        draft = self._draft(conv)
        resp = self.client.get(
            reverse('tickets:instagram_conversation_detail', args=[conv.id]))
        self.assertNotIn(draft, resp.context['thread_messages'])
        self.assertEqual(resp.context['pending_draft'], draft)

    def test_sent_messages_shown_in_thread(self):
        self._login_admin()
        conv = self._conversation()
        sent = InstagramMessage.objects.create(
            conversation=conv, direction=InstagramMessage.DIRECTION_OUTBOUND,
            author=InstagramMessage.AUTHOR_AGENT, content='Doors at 9pm.',
            status=InstagramMessage.STATUS_AUTO_SENT, provider_message_id='x')
        resp = self.client.get(
            reverse('tickets:instagram_conversation_detail', args=[conv.id]))
        self.assertIn(sent, resp.context['thread_messages'])


class InboxDraftApproveTests(_InboxViewTestBase):
    def test_approve_sends_and_marks_approved_then_resolves(self):
        self._login_admin()
        conv = self._conversation()
        draft = self._draft(conv)
        resp = self.client.post(
            reverse('tickets:instagram_draft_approve', args=[conv.id, draft.id]),
        )
        self.assertRedirects(
            resp, reverse('tickets:instagram_conversation_detail', args=[conv.id]))
        draft.refresh_from_db(); conv.refresh_from_db()
        self.assertEqual(draft.status, InstagramMessage.STATUS_APPROVED_SENT)
        self.assertEqual(draft.reviewed_by, self.admin_user)
        self.assertIsNotNone(draft.reviewed_at)
        self.assertTrue(draft.provider_message_id)  # stub sender returns an id
        self.assertEqual(conv.status, InstagramConversation.STATUS_RESOLVED)

    def test_approve_with_edit_sends_edited_text(self):
        self._login_admin()
        conv = self._conversation()
        draft = self._draft(conv, content='original')
        self.client.post(
            reverse('tickets:instagram_draft_approve', args=[conv.id, draft.id]),
            {'content': 'edited reply'},
        )
        draft.refresh_from_db()
        self.assertEqual(draft.content, 'edited reply')
        self.assertEqual(draft.status, InstagramMessage.STATUS_APPROVED_SENT)

    def test_discard_marks_discarded_no_send(self):
        self._login_admin()
        conv = self._conversation()
        draft = self._draft(conv)
        self.client.post(
            reverse('tickets:instagram_draft_approve', args=[conv.id, draft.id]),
            {'action': 'discard'},
        )
        draft.refresh_from_db(); conv.refresh_from_db()
        self.assertEqual(draft.status, InstagramMessage.STATUS_DISCARDED)
        self.assertEqual(draft.reviewed_by, self.admin_user)
        self.assertEqual(conv.status, InstagramConversation.STATUS_RESOLVED)

    def test_send_failure_marks_failed_and_keeps_awaiting(self):
        self._login_admin()
        conv = self._conversation()
        draft = self._draft(conv)
        failing = MagicMock()
        failing.send_text.return_value = SendResult(ok=False, error='window closed')
        with patch('tickets.integrations.instagram.get_sender', return_value=failing):
            self.client.post(
                reverse('tickets:instagram_draft_approve', args=[conv.id, draft.id]))
        draft.refresh_from_db(); conv.refresh_from_db()
        self.assertEqual(draft.status, InstagramMessage.STATUS_FAILED)
        # Not silently resolved — the organizer can retry.
        self.assertEqual(conv.status, InstagramConversation.STATUS_AWAITING_HUMAN)

    def test_cannot_reapprove_already_sent_draft(self):
        self._login_admin()
        conv = self._conversation()
        draft = self._draft(conv)
        draft.status = InstagramMessage.STATUS_APPROVED_SENT
        draft.save(update_fields=['status'])
        self.assertEqual(
            self.client.post(
                reverse('tickets:instagram_draft_approve', args=[conv.id, draft.id])
            ).status_code,
            404,
        )


class InboxHumanReplyTests(_InboxViewTestBase):
    def test_human_reply_sends_and_marks_human_handling(self):
        self._login_admin()
        conv = self._conversation()
        draft = self._draft(conv)
        self.client.post(
            reverse('tickets:instagram_message_send', args=[conv.id]),
            {'content': 'Hey, happy to help!'},
        )
        conv.refresh_from_db(); draft.refresh_from_db()
        reply = InstagramMessage.objects.get(
            conversation=conv, author=InstagramMessage.AUTHOR_HUMAN)
        self.assertEqual(reply.direction, InstagramMessage.DIRECTION_OUTBOUND)
        self.assertEqual(reply.status, InstagramMessage.STATUS_APPROVED_SENT)
        self.assertEqual(reply.reviewed_by, self.admin_user)
        # The thread is now human-owned (agent paused) and assigned to the replier —
        # it does NOT resolve, so the agent won't jump back in on the next message.
        self.assertEqual(conv.status, InstagramConversation.STATUS_HUMAN_HANDLING)
        self.assertEqual(conv.assigned_to, self.admin_user)
        # The pending AI draft is superseded by the human taking over.
        self.assertEqual(draft.status, InstagramMessage.STATUS_DISCARDED)

    def test_empty_reply_rejected(self):
        self._login_admin()
        conv = self._conversation()
        self.client.post(
            reverse('tickets:instagram_message_send', args=[conv.id]),
            {'content': '   '},
        )
        self.assertFalse(InstagramMessage.objects.filter(
            conversation=conv, author=InstagramMessage.AUTHOR_HUMAN).exists())


class InboxHandbackTests(_InboxViewTestBase):
    """Explicit 'hand back to agent' control on a human-owned thread."""

    def _human_owned(self):
        return self._conversation(
            status=InstagramConversation.STATUS_HUMAN_HANDLING,
            assigned_to=self.admin_user,
        )

    def test_handback_resumes_agent(self):
        self._login_admin()
        conv = self._human_owned()
        resp = self.client.post(
            reverse('tickets:instagram_conversation_handback', args=[conv.id]))
        self.assertRedirects(
            resp, reverse('tickets:instagram_conversation_detail', args=[conv.id]))
        conv.refresh_from_db()
        # Resolved re-enables the agent; ownership is cleared.
        self.assertEqual(conv.status, InstagramConversation.STATUS_RESOLVED)
        self.assertIsNone(conv.assigned_to)

    def test_handback_requires_post(self):
        self._login_admin()
        conv = self._human_owned()
        self.assertEqual(
            self.client.get(
                reverse('tickets:instagram_conversation_handback', args=[conv.id])
            ).status_code,
            405,
        )

    def test_handback_non_admin_forbidden(self):
        self._login_host()
        conv = self._human_owned()
        self.assertEqual(
            self.client.post(
                reverse('tickets:instagram_conversation_handback', args=[conv.id])
            ).status_code,
            403,
        )
        conv.refresh_from_db()
        self.assertEqual(conv.status, InstagramConversation.STATUS_HUMAN_HANDLING)

    def test_handback_other_org_404(self):
        self._login_admin()
        theirs = self._conversation(
            organization=self.other_org, ig_user_id='theirs',
            status=InstagramConversation.STATUS_HUMAN_HANDLING)
        self.assertEqual(
            self.client.post(
                reverse('tickets:instagram_conversation_handback', args=[theirs.id])
            ).status_code,
            404,
        )

    def test_inbox_ranks_human_handling_between_awaiting_and_open(self):
        self._login_admin()
        from django.utils import timezone
        now = timezone.now()
        # Equal last_message_at so ordering is driven purely by status rank.
        awaiting = self._conversation(
            ig_user_id='a', status=InstagramConversation.STATUS_AWAITING_HUMAN,
            last_message_at=now)
        human = self._conversation(
            ig_user_id='h', status=InstagramConversation.STATUS_HUMAN_HANDLING,
            last_message_at=now)
        opened = self._conversation(
            ig_user_id='o', status=InstagramConversation.STATUS_OPEN, last_message_at=now)
        resolved = self._conversation(
            ig_user_id='r', status=InstagramConversation.STATUS_RESOLVED,
            last_message_at=now)
        order = list(self.client.get(
            reverse('tickets:instagram_inbox')).context['conversations'])
        self.assertEqual(order, [awaiting, human, opened, resolved])

    def test_detail_shows_handback_button_only_when_human_owned(self):
        self._login_admin()
        human = self._human_owned()
        handback_url = reverse('tickets:instagram_conversation_handback', args=[human.id])
        # Shown in both spots: the header and down by the reply box (discoverability).
        self.assertContains(
            self.client.get(
                reverse('tickets:instagram_conversation_detail', args=[human.id])),
            handback_url, count=2)
        # Not shown on an awaiting-human thread (human hasn't taken over yet).
        awaiting = self._conversation(ig_user_id='aw2')
        self.assertNotContains(
            self.client.get(
                reverse('tickets:instagram_conversation_detail', args=[awaiting.id])),
            reverse('tickets:instagram_conversation_handback', args=[awaiting.id]))


@override_settings(INSTAGRAM_SENDER_BACKEND='stub')
class InboxEscalationNotifyTriggerTests(TestCase):
    """The orchestration task notifies once, only on the awaiting_human transition (D2)."""

    def setUp(self):
        self.org = Organization.objects.create(
            name='Notify Trigger Org', slug='notify-trigger-org',
            instagram_business_account_id='acct-n',
            instagram_support_agent_enabled=True,
        )

    def _normalized(self, text='I need a refund', mid='m1', sender='cust-1'):
        return {'ig_account_id': 'acct-n', 'sender_id': sender, 'text': text,
                'provider_message_id': mid, 'timestamp': 0}

    def _patch_pipeline(self):
        svc = patch('tickets.services.instagram.InstagramSupportAgentService')
        cls = svc.start(); self.addCleanup(svc.stop)
        cls.return_value.answer.return_value = AnswerResult(
            text='Let me check', tool_calls=['get_faq'])
        clf = patch('tickets.services.instagram.classify_escalation',
                    return_value=EscalationDecision(
                        should_escalate=True, confidence=0.9,
                        category='refund_dispute', reason='r'))
        clf.start(); self.addCleanup(clf.stop)

    def test_enqueues_once_on_transition_not_on_followup(self):
        from tickets.tasks import process_instagram_inbound_task
        self._patch_pipeline()
        with patch('tickets.tasks.notify_instagram_escalation_task.delay') as delay:
            with self.captureOnCommitCallbacks(execute=True):
                process_instagram_inbound_task.apply(
                    args=[str(self.org.id), self._normalized(mid='a')])
            # A second queued DM in the already-awaiting thread must not re-notify.
            with self.captureOnCommitCallbacks(execute=True):
                process_instagram_inbound_task.apply(
                    args=[str(self.org.id),
                          self._normalized(mid='b', text='still waiting')])
        self.assertEqual(delay.call_count, 1)


@override_settings(INSTAGRAM_SENDER_BACKEND='stub', SITE_URL='https://cue.test')
class InboxEscalationNotifyTaskTests(TestCase):
    """notify_instagram_escalation_task emails + pushes only org admins (D1)."""

    def setUp(self):
        self.org = Organization.objects.create(name='Notify Org', slug='notify-org')
        self.owner = User.objects.create_user('owner1', 'owner@example.com', 'pw')
        OrganizationMembership.objects.create(
            user=self.owner, organization=self.org,
            org_role=UserProfile.OrgRole.OWNER)
        self.host = User.objects.create_user('host1', 'host@example.com', 'pw')
        OrganizationMembership.objects.create(
            user=self.host, organization=self.org,
            org_role=UserProfile.OrgRole.HOST)
        self.conv = InstagramConversation.objects.create(
            organization=self.org, ig_user_id='cust-1',
            status=InstagramConversation.STATUS_AWAITING_HUMAN)
        InstagramMessage.objects.create(
            conversation=self.conv, direction=InstagramMessage.DIRECTION_INBOUND,
            author=InstagramMessage.AUTHOR_CUSTOMER, content='I need a refund',
            status=InstagramMessage.STATUS_RECEIVED)
        InstagramMessage.objects.create(
            conversation=self.conv, direction=InstagramMessage.DIRECTION_OUTBOUND,
            author=InstagramMessage.AUTHOR_AGENT, content='draft',
            status=InstagramMessage.STATUS_PENDING_REVIEW,
            escalation_category='refund_dispute')

    def test_emails_admins_only_and_pushes_admin_ids(self):
        from tickets.tasks import notify_instagram_escalation_task
        with patch(
            'tickets.services.push_notifications.dispatch.dispatch_to_users'
        ) as push:
            notify_instagram_escalation_task.apply(args=[str(self.conv.id)])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['owner@example.com'])  # host excluded
        push.assert_called_once()
        pushed_user_ids = push.call_args.args[1]
        self.assertIn(self.owner.id, pushed_user_ids)
        self.assertNotIn(self.host.id, pushed_user_ids)

    def test_no_admins_no_email(self):
        from tickets.tasks import notify_instagram_escalation_task
        OrganizationMembership.objects.filter(organization=self.org).delete()
        notify_instagram_escalation_task.apply(args=[str(self.conv.id)])
        self.assertEqual(len(mail.outbox), 0)


@override_settings(INSTAGRAM_SENDER_BACKEND='stub')
class IGEscalationAckTests(TestCase):
    """On escalation the customer gets an immediate, org-editable acknowledgement."""

    def setUp(self):
        self.org = Organization.objects.create(
            name='Ack Org', slug='ack-org',
            instagram_business_account_id='acct-ack',
            instagram_support_agent_enabled=True,
        )

    def _normalized(self, text='I need a refund', mid='m1', sender='cust-1'):
        return {'ig_account_id': 'acct-ack', 'sender_id': sender, 'text': text,
                'provider_message_id': mid, 'timestamp': 0}

    def _run(self, **kw):
        from tickets.tasks import process_instagram_inbound_task
        process_instagram_inbound_task.apply(args=[str(self.org.id), self._normalized(**kw)])

    def _patch_escalate(self):
        svc = patch('tickets.services.instagram.InstagramSupportAgentService')
        cls = svc.start(); self.addCleanup(svc.stop)
        cls.return_value.answer.return_value = AnswerResult(text='draft', tool_calls=['get_faq'])
        clf = patch('tickets.services.instagram.classify_escalation',
                    return_value=EscalationDecision(should_escalate=True, confidence=0.9,
                                                    category='refund_dispute', reason='r'))
        clf.start(); self.addCleanup(clf.stop)

    def test_escalation_sends_ack_and_no_agent_draft(self):
        self._patch_escalate()
        self._run()
        conv = InstagramConversation.objects.get(organization=self.org, ig_user_id='cust-1')
        # No agent draft on a true escalation — the human writes the real reply.
        self.assertFalse(InstagramMessage.objects.filter(
            conversation=conv, author=InstagramMessage.AUTHOR_AGENT,
            direction=InstagramMessage.DIRECTION_OUTBOUND).exists())
        # But the customer still got an immediate acknowledgement.
        ack = InstagramMessage.objects.get(
            conversation=conv, author=InstagramMessage.AUTHOR_SYSTEM)
        self.assertEqual(ack.status, InstagramMessage.STATUS_AUTO_SENT)
        self.assertEqual(ack.content, self.org.instagram_escalation_ack_text)
        self.assertTrue(ack.provider_message_id)  # actually sent via the stub

    def test_guardrail_queue_keeps_draft_without_ack(self):
        # A routine answer that couldn't auto-send (here: ungrounded, no tool hit) is
        # queued for approval — keep the draft and flag the thread, but DON'T ack the
        # customer: the human just approves the AI answer, no follow-up is promised.
        svc = patch('tickets.services.instagram.InstagramSupportAgentService')
        cls = svc.start(); self.addCleanup(svc.stop)
        cls.return_value.answer.return_value = AnswerResult(text='Doors at 9pm', tool_calls=[])
        clf = patch('tickets.services.instagram.classify_escalation',
                    return_value=EscalationDecision(should_escalate=False, confidence=0.95,
                                                    category='routine', reason='r'))
        clf.start(); self.addCleanup(clf.stop)
        self._run()
        conv = InstagramConversation.objects.get(organization=self.org, ig_user_id='cust-1')
        self.assertEqual(conv.status, InstagramConversation.STATUS_AWAITING_HUMAN)
        self.assertTrue(InstagramMessage.objects.filter(
            conversation=conv, author=InstagramMessage.AUTHOR_AGENT,
            status=InstagramMessage.STATUS_PENDING_REVIEW).exists())
        self.assertFalse(InstagramMessage.objects.filter(
            conversation=conv, author=InstagramMessage.AUTHOR_SYSTEM).exists())

    def test_blank_ack_text_sends_nothing(self):
        self.org.instagram_escalation_ack_text = ''
        self.org.save(update_fields=['instagram_escalation_ack_text'])
        self._patch_escalate()
        self._run()
        conv = InstagramConversation.objects.get(organization=self.org, ig_user_id='cust-1')
        self.assertFalse(InstagramMessage.objects.filter(
            conversation=conv, author=InstagramMessage.AUTHOR_SYSTEM).exists())

    def test_ack_sent_once_per_transition(self):
        self._patch_escalate()
        self._run(mid='a')
        self._run(mid='b', text='still waiting')
        conv = InstagramConversation.objects.get(organization=self.org, ig_user_id='cust-1')
        self.assertEqual(InstagramMessage.objects.filter(
            conversation=conv, author=InstagramMessage.AUTHOR_SYSTEM).count(), 1)

    def test_no_new_draft_while_awaiting_human(self):
        # First DM escalates -> awaiting_human (+ ack). A follow-up on the now
        # human-owned thread is recorded but the agent must not draft over the human.
        self._patch_escalate()
        self._run(mid='first')
        conv = InstagramConversation.objects.get(organization=self.org, ig_user_id='cust-1')
        agent_drafts_before = InstagramMessage.objects.filter(
            conversation=conv, direction=InstagramMessage.DIRECTION_OUTBOUND,
            author=InstagramMessage.AUTHOR_AGENT).count()
        self._run(mid='second', text='okay')
        conv.refresh_from_db()
        self.assertEqual(conv.status, InstagramConversation.STATUS_AWAITING_HUMAN)
        # Inbound recorded so the human sees it...
        self.assertTrue(InstagramMessage.objects.filter(
            conversation=conv, direction=InstagramMessage.DIRECTION_INBOUND,
            content='okay').exists())
        # ...but no new agent draft was produced.
        self.assertEqual(
            InstagramMessage.objects.filter(
                conversation=conv, direction=InstagramMessage.DIRECTION_OUTBOUND,
                author=InstagramMessage.AUTHOR_AGENT).count(),
            agent_drafts_before,
        )

    @override_settings(IG_AGENT_DAILY_ANSWER_CAP=1)
    def test_ack_does_not_count_against_daily_cap(self):
        # A prior automated ack must not consume the auto-answer budget.
        conv = InstagramConversation.objects.create(
            organization=self.org, ig_user_id='cust-2')
        InstagramMessage.objects.create(
            conversation=conv, direction=InstagramMessage.DIRECTION_OUTBOUND,
            author=InstagramMessage.AUTHOR_SYSTEM, content='ack',
            status=InstagramMessage.STATUS_AUTO_SENT, provider_message_id='ackprev')
        svc = patch('tickets.services.instagram.InstagramSupportAgentService')
        cls = svc.start(); self.addCleanup(svc.stop)
        cls.return_value.answer.return_value = AnswerResult(
            text='Doors at 9pm', tool_calls=['get_faq'])
        clf = patch('tickets.services.instagram.classify_escalation',
                    return_value=EscalationDecision(should_escalate=False, confidence=0.95,
                                                    category='routine', reason='r'))
        clf.start(); self.addCleanup(clf.stop)
        self._run(sender='cust-2', mid='routine-1', text='when do doors open?')
        answer = InstagramMessage.objects.get(
            conversation=conv, author=InstagramMessage.AUTHOR_AGENT,
            direction=InstagramMessage.DIRECTION_OUTBOUND)
        self.assertEqual(answer.status, InstagramMessage.STATUS_AUTO_SENT)


class InstagramAgentSettingsViewTests(_FAQViewTestBase):
    def test_admin_can_update_ack_text(self):
        self._login_admin()
        resp = self.client.post(
            reverse('tickets:instagram_agent_settings'),
            {'escalation_ack_text': 'We will get back to you soon!'})
        self.assertRedirects(resp, reverse('tickets:instagram_faq_list'))
        self.org.refresh_from_db()
        self.assertEqual(
            self.org.instagram_escalation_ack_text, 'We will get back to you soon!')

    def test_non_admin_forbidden(self):
        self._login_host()
        self.assertEqual(
            self.client.post(
                reverse('tickets:instagram_agent_settings'),
                {'escalation_ack_text': 'x'}).status_code,
            403,
        )
        self.org.refresh_from_db()
        self.assertNotEqual(self.org.instagram_escalation_ack_text, 'x')
