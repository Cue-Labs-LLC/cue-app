"""Phase 0 model tests for the Instagram DM support agent.

Covers the data-model foundations only: OrgFAQ, InstagramConversation,
InstagramMessage, the new Organization integration fields, and the new
AITokenUsage feature constant. No behavior/views yet.
"""

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase

from .models import (
    AITokenUsage,
    InstagramConversation,
    InstagramMessage,
    OrgFAQ,
    Organization,
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
