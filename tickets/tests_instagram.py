"""Phase 0 model tests for the Instagram DM support agent.

Covers the data-model foundations only: OrgFAQ, InstagramConversation,
InstagramMessage, the new Organization integration fields, and the new
AITokenUsage feature constant. No behavior/views yet.
"""

import json
from io import StringIO
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse

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
from .services.instagram import FaqAnswer, answer_faq


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


@override_settings(OPENAI_API_KEY='test-key', OPENAI_MODEL='gpt-4o')
class FAQAnswerAgentTests(_FAQViewTestBase):
    """answer_faq() with the LLM mocked (patch at the import source)."""

    def _fake_llm(self, parsed, input_tokens=40, output_tokens=20):
        raw = MagicMock()
        raw.usage_metadata = {
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'total_tokens': input_tokens + output_tokens,
        }
        structured = MagicMock()
        structured.invoke.return_value = {'raw': raw, 'parsed': parsed, 'parsing_error': None}
        llm = MagicMock()
        llm.with_structured_output.return_value = structured
        return llm, structured

    @patch('langchain_openai.ChatOpenAI')
    def test_answers_faq_and_meters(self, mock_openai):
        faq = self._faq(question='How do I reach a human?', answer='Just reply here.')
        parsed = FaqAnswer(answered=True, answer='Just reply here!',
                           matched_faq_id=str(faq.id), confidence=0.9)
        mock_openai.return_value = self._fake_llm(parsed)[0]

        result = answer_faq(self.org, question='can I talk to someone?')
        self.assertTrue(result.answered)
        self.assertEqual(result.matched_faq_id, str(faq.id))

        usage = AITokenUsage.objects.get(organization=self.org)
        self.assertEqual(usage.feature, AITokenUsage.FEATURE_IG_SUPPORT_AGENT)
        self.assertEqual(usage.metadata.get('stage'), 'answer')
        self.assertIsNone(usage.user)
        self.assertEqual(usage.total_tokens, 60)

    @patch('langchain_openai.ChatOpenAI')
    def test_only_published_faqs_reach_the_prompt(self, mock_openai):
        self._faq(question='PUBLISHED_MARKER question', answer='PUBLISHED_ANSWER_MARKER')
        self._faq(question='HIDDEN_MARKER question', answer='HIDDEN_ANSWER_MARKER',
                  is_published=False)
        _, structured = self._fake_llm(FaqAnswer(answered=False, answer='A human will follow up.'))
        mock_openai.return_value.with_structured_output.return_value = structured

        answer_faq(self.org, question='anything')

        sent = json.dumps(structured.invoke.call_args[0][0])
        self.assertIn('PUBLISHED_MARKER', sent)
        self.assertNotIn('HIDDEN_MARKER', sent)
        self.assertNotIn('HIDDEN_ANSWER_MARKER', sent)

    @patch('langchain_openai.ChatOpenAI')
    def test_declines_when_no_faq_matches(self, mock_openai):
        self._faq(question='How do I buy tickets?', answer='Use the link.')
        parsed = FaqAnswer(answered=False, answer='A team member will follow up.',
                           matched_faq_id=None, confidence=0.0)
        mock_openai.return_value = self._fake_llm(parsed)[0]
        result = answer_faq(self.org, question='can I get a refund?')
        self.assertFalse(result.answered)
        self.assertIsNone(result.matched_faq_id)

    @patch('langchain_openai.ChatOpenAI')
    def test_management_command_prints_answer(self, mock_openai):
        self._faq(question='How do I reach a human?', answer='Reply here.')
        parsed = FaqAnswer(answered=True, answer='Reply here!', confidence=0.8)
        mock_openai.return_value = self._fake_llm(parsed)[0]
        out = StringIO()
        call_command('answer_ig_faq', '--org', self.org.slug,
                     '--question', 'can I talk to someone?', stdout=out)
        self.assertIn('Reply here!', out.getvalue())
