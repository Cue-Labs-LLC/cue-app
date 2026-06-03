"""Tests for native marketing SMS: recipient resolution, sending, scheduling,
webhooks, views, and analytics."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    Organization, UserProfile, Customer, CustomerTag,
    SMSCampaign, SMSRecipientList, SMSMessageRecipient, PhoneSuppression,
)


def make_customer(org, email, phone='+13105550000', opt_in=True, **kwargs):
    return Customer.objects.create(
        organization=org, email=email, name=kwargs.pop('name', email.split('@')[0]),
        phone=phone, sms_opt_in=opt_in, **kwargs,
    )


@override_settings(E2E_TEST_MODE=True, SMS_CAMPAIGN_MAX_RECIPIENTS=5000)
class SMSRecipientListResolveTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Org A', slug='org-a', sms_marketing_enabled=True)

    def _list(self, **kwargs):
        return SMSRecipientList.objects.create(organization=self.org, name='L', **kwargs)

    def test_only_opted_in_with_phone(self):
        make_customer(self.org, 'a@x.com', '+13105550001', opt_in=True)
        make_customer(self.org, 'b@x.com', '+13105550002', opt_in=False)   # not opted in
        make_customer(self.org, 'c@x.com', '', opt_in=True)                 # no phone
        rl = self._list(filter_criteria={'min_ltv': '0'})
        phones = {r['phone'] for r in rl.materialize(self.org)}
        self.assertEqual(phones, {'+13105550001'})

    def test_excludes_suppressed_global_and_org(self):
        make_customer(self.org, 'a@x.com', '+13105550001')
        make_customer(self.org, 'b@x.com', '+13105550002')
        PhoneSuppression.objects.create(phone='+13105550001', organization=None)        # global
        PhoneSuppression.objects.create(phone='+13105550002', organization=self.org)    # org
        rl = self._list(filter_criteria={'min_ltv': '0'})
        self.assertEqual(rl.materialize(self.org), [])

    def test_dedupe_by_phone(self):
        # Same number, two customer rows (different email) -> one recipient.
        make_customer(self.org, 'a@x.com', '3105550009')
        make_customer(self.org, 'b@x.com', '+13105550009')
        rl = self._list(filter_criteria={'min_ltv': '0'})
        self.assertEqual(len(rl.materialize(self.org)), 1)

    def test_manual_include_only_works(self):
        c = make_customer(self.org, 'a@x.com', '+13105550001')
        rl = self._list(filter_criteria={}, manual_include_ids=[str(c.id)])
        self.assertEqual(len(rl.materialize(self.org)), 1)

    def test_manual_exclude_wins(self):
        make_customer(self.org, 'a@x.com', '+13105550001', rfm_segment='VIP')
        c2 = make_customer(self.org, 'b@x.com', '+13105550002', rfm_segment='VIP')
        rl = self._list(filter_criteria={'rfm_segment': ['VIP']}, manual_exclude_ids=[str(c2.id)])
        phones = {r['phone'] for r in rl.materialize(self.org)}
        self.assertEqual(phones, {'+13105550001'})

    def test_empty_criteria_and_no_includes_is_empty(self):
        make_customer(self.org, 'a@x.com', '+13105550001')
        rl = self._list(filter_criteria={}, manual_include_ids=[])
        self.assertEqual(rl.materialize(self.org), [])

    def test_cap_is_enforced(self):
        for i in range(5):
            make_customer(self.org, f'c{i}@x.com', f'+1310555100{i}')
        rl = self._list(filter_criteria={'min_ltv': '0'})
        self.assertEqual(len(rl.materialize(self.org, cap=3)), 3)

    def test_suppression_is_suppressed_helper(self):
        PhoneSuppression.objects.create(phone='+13105550001', organization=None)
        self.assertTrue(PhoneSuppression.is_suppressed('+13105550001', self.org))
        self.assertFalse(PhoneSuppression.is_suppressed('+13105559999', self.org))


class FilterCustomersRegressionTests(TestCase):
    """The customer_list view was refactored onto filter_customers — make sure
    its segment/tag/search behavior is unchanged."""
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Org', slug='org-reg')
        self.user = User.objects.create_user('u', 'u@test.com', 'pw')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='u@test.com', password='pw')
        self.client.get(reverse('tickets:home'))
        self.tag = CustomerTag.objects.create(organization=self.org, name='VIP')
        self.vip = make_customer(self.org, 'vip@x.com', name='Vippy', rfm_segment='VIP')
        self.vip.tags.add(self.tag)
        make_customer(self.org, 'reg@x.com', name='Reggie', rfm_segment='Loyal')

    def test_segment_filter(self):
        resp = self.client.get(reverse('tickets:customer_list'), {'segment': 'VIP'})
        self.assertEqual(resp.status_code, 200)
        emails = [c.email for c in resp.context['page_obj']]
        self.assertEqual(emails, ['vip@x.com'])

    def test_search_filter(self):
        resp = self.client.get(reverse('tickets:customer_list'), {'search': 'Reggie'})
        self.assertEqual([c.email for c in resp.context['page_obj']], ['reg@x.com'])

    def test_tag_filter(self):
        resp = self.client.get(reverse('tickets:customer_list'), {'tag': str(self.tag.id)})
        self.assertEqual([c.email for c in resp.context['page_obj']], ['vip@x.com'])

    def test_bad_tag_uuid_ignored(self):
        resp = self.client.get(reverse('tickets:customer_list'), {'tag': 'not-a-uuid'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['tag_filter'], '')


@override_settings(E2E_TEST_MODE=True, SMS_CAMPAIGN_MAX_RECIPIENTS=5000)
class SMSCampaignSendTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Org', slug='org-send', sms_marketing_enabled=True)
        for i in range(3):
            make_customer(self.org, f'c{i}@x.com', f'+1310555200{i}')
        self.rl = SMSRecipientList.objects.create(
            organization=self.org, name='All', filter_criteria={'min_ltv': '0'},
        )

    def _campaign(self, **kwargs):
        return SMSCampaign.objects.create(
            organization=self.org, recipient_list=self.rl, name='C', body='Hi there',
            status=SMSCampaign.Status.DRAFT, **kwargs,
        )

    def test_send_marks_sent_and_snapshots(self):
        from .tasks import send_sms_campaign_task
        c = self._campaign()
        send_sms_campaign_task.delay(str(c.id))
        c.refresh_from_db()
        self.assertEqual(c.status, SMSCampaign.Status.SENT)
        self.assertEqual(c.audience_size, 3)
        self.assertEqual(
            SMSMessageRecipient.objects.filter(campaign=c, status=SMSMessageRecipient.Status.SENT).count(), 3,
        )
        self.assertIsNotNone(c.sent_at)

    def test_idempotent_no_double_send(self):
        from .tasks import send_sms_campaign_task
        c = self._campaign()
        send_sms_campaign_task.delay(str(c.id))
        send_sms_campaign_task.delay(str(c.id))  # re-dispatch
        self.assertEqual(SMSMessageRecipient.objects.filter(campaign=c).count(), 3)

    def test_failure_isolation(self):
        from .tasks import send_sms_campaign_task

        def fake_send(to, body, status_callback=None):
            if to == '+13105552001':
                return False, None
            return True, 'SM' + to[-4:]

        c = self._campaign()
        with patch('tickets.sms.send_sms', side_effect=fake_send):
            send_sms_campaign_task.delay(str(c.id))
        self.assertEqual(SMSMessageRecipient.objects.filter(campaign=c, status='failed').count(), 1)
        self.assertEqual(SMSMessageRecipient.objects.filter(campaign=c, status='sent').count(), 2)

    def test_stop_footer_appended(self):
        from .tasks import _with_stop_footer
        self.assertIn('Reply STOP to opt out', _with_stop_footer('Hello'))
        self.assertEqual(_with_stop_footer('Text STOP anytime'), 'Text STOP anytime')


@override_settings(E2E_TEST_MODE=True)
class SMSSchedulerCommandTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Org', slug='org-sch', sms_marketing_enabled=True)
        make_customer(self.org, 'a@x.com', '+13105553001')
        self.rl = SMSRecipientList.objects.create(
            organization=self.org, name='All', filter_criteria={'min_ltv': '0'},
        )

    def _campaign(self, status, scheduled_at=None, started_at=None):
        return SMSCampaign.objects.create(
            organization=self.org, recipient_list=self.rl, name='C', body='Hi',
            status=status, scheduled_at=scheduled_at, started_at=started_at,
        )

    def test_due_scheduled_is_sent(self):
        c = self._campaign(SMSCampaign.Status.SCHEDULED, scheduled_at=timezone.now() - timedelta(minutes=1))
        call_command('send_due_sms_campaigns')
        c.refresh_from_db()
        self.assertEqual(c.status, SMSCampaign.Status.SENT)

    def test_future_scheduled_is_skipped(self):
        c = self._campaign(SMSCampaign.Status.SCHEDULED, scheduled_at=timezone.now() + timedelta(hours=2))
        call_command('send_due_sms_campaigns')
        c.refresh_from_db()
        self.assertEqual(c.status, SMSCampaign.Status.SCHEDULED)

    def test_canceled_is_not_sent(self):
        c = self._campaign(SMSCampaign.Status.CANCELED, scheduled_at=timezone.now() - timedelta(minutes=1))
        call_command('send_due_sms_campaigns')
        c.refresh_from_db()
        self.assertEqual(c.status, SMSCampaign.Status.CANCELED)

    def test_stuck_sending_is_recovered(self):
        c = self._campaign(SMSCampaign.Status.SENDING, started_at=timezone.now() - timedelta(minutes=30))
        # A queued recipient remains (worker died mid-send).
        SMSMessageRecipient.objects.create(campaign=c, phone='+13105553001', status='queued')
        call_command('send_due_sms_campaigns')
        c.refresh_from_db()
        self.assertEqual(c.status, SMSCampaign.Status.SENT)
        self.assertFalse(
            SMSMessageRecipient.objects.filter(campaign=c, status='queued').exists()
        )


class SMSWebhookTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Org', slug='org-wh', sms_marketing_enabled=True)
        self.rl = SMSRecipientList.objects.create(
            organization=self.org, name='All', filter_criteria={'min_ltv': '0'},
        )
        self.campaign = SMSCampaign.objects.create(
            organization=self.org, recipient_list=self.rl, name='C', body='Hi',
            status=SMSCampaign.Status.SENT,
        )
        self.recipient = SMSMessageRecipient.objects.create(
            campaign=self.campaign, phone='+13105554001', status='sent', twilio_sid='SM123',
        )

    @override_settings(TWILIO_VALIDATE_WEBHOOKS=False, E2E_TEST_MODE=False)
    def test_status_webhook_updates_delivered(self):
        resp = self.client.post(reverse('tickets:twilio_sms_status_webhook'), {
            'MessageSid': 'SM123', 'MessageStatus': 'delivered',
        })
        self.assertEqual(resp.status_code, 200)
        self.recipient.refresh_from_db()
        self.assertEqual(self.recipient.status, 'delivered')
        self.assertIsNotNone(self.recipient.delivered_at)

    @override_settings(TWILIO_VALIDATE_WEBHOOKS=False, E2E_TEST_MODE=False)
    def test_status_webhook_idempotent_no_drift(self):
        url = reverse('tickets:twilio_sms_status_webhook')
        for _ in range(3):
            self.client.post(url, {'MessageSid': 'SM123', 'MessageStatus': 'delivered'})
        delivered = SMSMessageRecipient.objects.filter(campaign=self.campaign, status='delivered').count()
        self.assertEqual(delivered, 1)

    @override_settings(TWILIO_VALIDATE_WEBHOOKS=False, E2E_TEST_MODE=False)
    def test_status_webhook_unknown_sid_is_noop(self):
        resp = self.client.post(reverse('tickets:twilio_sms_status_webhook'), {
            'MessageSid': 'SM_UNKNOWN', 'MessageStatus': 'delivered',
        })
        self.assertEqual(resp.status_code, 200)

    @override_settings(TWILIO_VALIDATE_WEBHOOKS=True, E2E_TEST_MODE=False)
    def test_status_webhook_bad_signature_403(self):
        resp = self.client.post(reverse('tickets:twilio_sms_status_webhook'), {
            'MessageSid': 'SM123', 'MessageStatus': 'delivered',
        })
        self.assertEqual(resp.status_code, 403)

    @override_settings(TWILIO_VALIDATE_WEBHOOKS=False, E2E_TEST_MODE=False)
    def test_inbound_stop_suppresses_globally(self):
        resp = self.client.post(reverse('tickets:twilio_sms_inbound_webhook'), {
            'From': '+13105554001', 'OptOutType': 'STOP', 'Body': 'STOP',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            PhoneSuppression.objects.filter(phone='+13105554001', organization__isnull=True).exists()
        )

    @override_settings(TWILIO_VALIDATE_WEBHOOKS=False, E2E_TEST_MODE=False)
    def test_inbound_start_removes_suppression(self):
        PhoneSuppression.objects.create(phone='+13105554001', organization=None)
        self.client.post(reverse('tickets:twilio_sms_inbound_webhook'), {
            'From': '+13105554001', 'OptOutType': 'START', 'Body': 'START',
        })
        self.assertFalse(
            PhoneSuppression.objects.filter(phone='+13105554001', organization__isnull=True).exists()
        )

    @override_settings(TWILIO_VALIDATE_WEBHOOKS=True, E2E_TEST_MODE=False)
    def test_inbound_bad_signature_403(self):
        resp = self.client.post(reverse('tickets:twilio_sms_inbound_webhook'), {
            'From': '+13105554001', 'OptOutType': 'STOP',
        })
        self.assertEqual(resp.status_code, 403)


@override_settings(E2E_TEST_MODE=True, SMS_CAMPAIGN_MAX_RECIPIENTS=5000)
class SMSViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Org A', slug='org-va', sms_marketing_enabled=True)
        self.user = User.objects.create_user('u', 'u@test.com', 'pw')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='u@test.com', password='pw')
        self.client.get(reverse('tickets:home'))
        make_customer(self.org, 'a@x.com', '+13105555001')
        self.rl = SMSRecipientList.objects.create(
            organization=self.org, name='All', filter_criteria={'min_ltv': '0'},
        )

    def test_feature_gate_blocks_when_disabled(self):
        self.org.sms_marketing_enabled = False
        self.org.save(update_fields=['sms_marketing_enabled'])
        resp = self.client.get(reverse('tickets:sms_campaign_list'))
        self.assertEqual(resp.status_code, 404)

    def test_campaign_list_ok_when_enabled(self):
        resp = self.client.get(reverse('tickets:sms_campaign_list'))
        self.assertEqual(resp.status_code, 200)

    def test_preview_returns_count(self):
        resp = self.client.post(reverse('tickets:sms_recipient_list_preview'), {'min_ltv': '0'})
        self.assertEqual(resp.json()['count'], 1)

    def test_create_requires_confirm_before_send(self):
        # First POST without confirm: shows count, does NOT create.
        resp = self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': 'Promo', 'recipient_list': str(self.rl.id), 'body': 'Hello',
            'send_mode': 'now',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['confirm_count'], 1)
        self.assertEqual(SMSCampaign.objects.count(), 0)

    def test_create_with_confirm_sends(self):
        resp = self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': 'Promo', 'recipient_list': str(self.rl.id), 'body': 'Hello',
            'send_mode': 'now', 'confirm': '1',
        })
        self.assertEqual(resp.status_code, 302)
        c = SMSCampaign.objects.get()
        self.assertEqual(c.status, SMSCampaign.Status.SENT)

    def test_schedule_creates_scheduled_campaign(self):
        when = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        resp = self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': 'Later', 'recipient_list': str(self.rl.id), 'body': 'Hello',
            'send_mode': 'schedule', 'scheduled_at': when, 'confirm': '1',
        })
        self.assertEqual(resp.status_code, 302)
        c = SMSCampaign.objects.get()
        self.assertEqual(c.status, SMSCampaign.Status.SCHEDULED)
        self.assertIsNotNone(c.scheduled_at)

    def test_cancel_scheduled(self):
        c = SMSCampaign.objects.create(
            organization=self.org, recipient_list=self.rl, name='S', body='Hi',
            status=SMSCampaign.Status.SCHEDULED, scheduled_at=timezone.now() + timedelta(days=1),
        )
        resp = self.client.post(reverse('tickets:sms_campaign_cancel', kwargs={'pk': c.id}))
        self.assertEqual(resp.status_code, 302)
        c.refresh_from_db()
        self.assertEqual(c.status, SMSCampaign.Status.CANCELED)

    def test_cross_tenant_detail_404(self):
        other = Organization.objects.create(name='Org B', slug='org-vb', sms_marketing_enabled=True)
        orl = SMSRecipientList.objects.create(organization=other, name='X', filter_criteria={'min_ltv': '0'})
        oc = SMSCampaign.objects.create(
            organization=other, recipient_list=orl, name='Theirs', body='Hi',
            status=SMSCampaign.Status.DRAFT,
        )
        resp = self.client.get(reverse('tickets:sms_campaign_detail', kwargs={'pk': oc.id}))
        self.assertEqual(resp.status_code, 404)


@override_settings(E2E_TEST_MODE=True)
class SMSAnalyticsTests(TestCase):
    def test_native_summary_counts(self):
        from .services.marketing.analytics import MarketingAnalyticsService
        org = Organization.objects.create(name='Org', slug='org-an', sms_marketing_enabled=True)
        rl = SMSRecipientList.objects.create(organization=org, name='All', filter_criteria={'min_ltv': '0'})
        c = SMSCampaign.objects.create(
            organization=org, recipient_list=rl, name='C', body='Hi',
            status=SMSCampaign.Status.SENT, sent_at=timezone.now(), audience_size=2,
        )
        SMSMessageRecipient.objects.create(campaign=c, phone='+13105556001', status='delivered')
        SMSMessageRecipient.objects.create(campaign=c, phone='+13105556002', status='failed')
        PhoneSuppression.objects.create(phone='+13105556003', organization=None)

        summary = MarketingAnalyticsService(org, window_days=90).calculate()['native_sms']
        self.assertEqual(summary['campaigns_sent'], 1)
        self.assertEqual(summary['messages_delivered'], 1)
        self.assertEqual(summary['messages_failed'], 1)
        self.assertEqual(summary['opt_outs'], 1)


@override_settings(E2E_TEST_MODE=True)
class SMSLinkExtractionTests(TestCase):
    def test_extract_first_url(self):
        from .sms import extract_first_url
        self.assertEqual(extract_first_url('Sale at https://shop.co/x today!'), 'https://shop.co/x')
        self.assertEqual(extract_first_url('Two https://a.co/1 and http://b.co/2'), 'https://a.co/1')
        self.assertEqual(extract_first_url('no link here, visit shop.co'), '')
        self.assertEqual(extract_first_url('end https://a.co/p.'), 'https://a.co/p')


@override_settings(E2E_TEST_MODE=True, SMS_CAMPAIGN_MAX_RECIPIENTS=5000)
class SMSLinkRewriteTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Org', slug='org-lr', sms_marketing_enabled=True)
        for i in range(2):
            make_customer(self.org, f'c{i}@x.com', f'+1310555700{i}')
        self.rl = SMSRecipientList.objects.create(
            organization=self.org, name='All', filter_criteria={'min_ltv': '0'},
        )

    def _campaign(self, body, link_url):
        return SMSCampaign.objects.create(
            organization=self.org, recipient_list=self.rl, name='C', body=body,
            link_url=link_url, status=SMSCampaign.Status.DRAFT,
        )

    @override_settings(SITE_URL='https://app.example.com')
    def test_public_site_assigns_token_and_rewrites_body(self):
        from .tasks import send_sms_campaign_task
        c = self._campaign('Sale at https://shop.co/x now', 'https://shop.co/x')
        sent_bodies = []

        def capture(to, body, status_callback=None):
            sent_bodies.append(body)
            return True, 'SID' + to[-4:]

        with patch('tickets.sms.send_sms', side_effect=capture):
            send_sms_campaign_task.delay(str(c.id))

        tokens = list(
            SMSMessageRecipient.objects.filter(campaign=c).values_list('click_token', flat=True)
        )
        self.assertEqual(len([t for t in tokens if t]), 2)
        self.assertEqual(len(set(tokens)), 2)  # unique per recipient
        for body in sent_bodies:
            self.assertNotIn('https://shop.co/x', body)
            self.assertIn('/c/', body)

    @override_settings(SITE_URL='http://localhost:8000')
    def test_local_site_skips_tracking(self):
        from .tasks import send_sms_campaign_task
        c = self._campaign('Sale at https://shop.co/x now', 'https://shop.co/x')
        sent_bodies = []
        with patch('tickets.sms.send_sms', side_effect=lambda to, body, status_callback=None: (sent_bodies.append(body), (True, 'SID'))[1]):
            send_sms_campaign_task.delay(str(c.id))
        self.assertFalse(SMSMessageRecipient.objects.filter(campaign=c).exclude(click_token__isnull=True).exists())
        self.assertTrue(all('https://shop.co/x' in b for b in sent_bodies))

    @override_settings(SITE_URL='https://app.example.com')
    def test_no_url_no_token(self):
        from .tasks import send_sms_campaign_task
        c = self._campaign('Just a plain message', '')
        send_sms_campaign_task.delay(str(c.id))
        self.assertFalse(SMSMessageRecipient.objects.filter(campaign=c, click_token__isnull=False).exists())


@override_settings(E2E_TEST_MODE=True)
class SMSClickRedirectTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Org', slug='org-cr', sms_marketing_enabled=True)
        self.rl = SMSRecipientList.objects.create(organization=self.org, name='All', filter_criteria={'min_ltv': '0'})
        self.campaign = SMSCampaign.objects.create(
            organization=self.org, recipient_list=self.rl, name='C', body='Hi https://shop.co/x',
            link_url='https://shop.co/x', status=SMSCampaign.Status.SENT,
        )
        self.recipient = SMSMessageRecipient.objects.create(
            campaign=self.campaign, phone='+13105558001', status='delivered', click_token='tok123',
        )

    def test_click_records_and_redirects(self):
        resp = self.client.get(reverse('tickets:sms_click_redirect', kwargs={'token': 'tok123'}))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], 'https://shop.co/x')
        self.recipient.refresh_from_db()
        self.assertEqual(self.recipient.click_count, 1)
        self.assertIsNotNone(self.recipient.first_clicked_at)

    def test_repeat_clicks_total_up_unique_once(self):
        url = reverse('tickets:sms_click_redirect', kwargs={'token': 'tok123'})
        self.client.get(url)
        self.recipient.refresh_from_db()
        first = self.recipient.first_clicked_at
        self.client.get(url)
        self.recipient.refresh_from_db()
        self.assertEqual(self.recipient.click_count, 2)
        self.assertEqual(self.recipient.first_clicked_at, first)  # unchanged

    def test_unknown_token_404(self):
        resp = self.client.get(reverse('tickets:sms_click_redirect', kwargs={'token': 'nope'}))
        self.assertEqual(resp.status_code, 404)


@override_settings(E2E_TEST_MODE=True)
class SMSUnsubAttributionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Org', slug='org-un', sms_marketing_enabled=True)
        self.rl = SMSRecipientList.objects.create(organization=self.org, name='All', filter_criteria={'min_ltv': '0'})

    def _recipient(self, sent_offset_min, campaign=None):
        c = campaign or SMSCampaign.objects.create(
            organization=self.org, recipient_list=self.rl, name='C', body='Hi',
            status=SMSCampaign.Status.SENT,
        )
        return SMSMessageRecipient.objects.create(
            campaign=c, phone='+13105559001', status='delivered',
            sent_at=timezone.now() - timedelta(minutes=sent_offset_min),
        )

    @override_settings(TWILIO_VALIDATE_WEBHOOKS=False, E2E_TEST_MODE=False)
    def test_stop_attributes_to_most_recent_recipient(self):
        old = self._recipient(60)
        recent = self._recipient(2)
        resp = self.client.post(reverse('tickets:twilio_sms_inbound_webhook'), {
            'From': '+13105559001', 'OptOutType': 'STOP', 'Body': 'STOP',
        })
        self.assertEqual(resp.status_code, 200)
        old.refresh_from_db(); recent.refresh_from_db()
        self.assertIsNotNone(recent.opted_out_at)
        self.assertIsNone(old.opted_out_at)
        self.assertTrue(PhoneSuppression.objects.filter(phone='+13105559001', organization__isnull=True).exists())


@override_settings(E2E_TEST_MODE=True)
class SMSClickMetricsTests(TestCase):
    def test_annotate_and_summary_counts(self):
        from .sms_views import _annotate_counts
        from .services.marketing.analytics import MarketingAnalyticsService
        org = Organization.objects.create(name='Org', slug='org-cm', sms_marketing_enabled=True)
        rl = SMSRecipientList.objects.create(organization=org, name='All', filter_criteria={'min_ltv': '0'})
        c = SMSCampaign.objects.create(
            organization=org, recipient_list=rl, name='C', body='Hi https://s.co/x',
            link_url='https://s.co/x', status=SMSCampaign.Status.SENT, sent_at=timezone.now(),
        )
        SMSMessageRecipient.objects.create(campaign=c, phone='+13105550101', status='delivered',
                                           click_count=3, first_clicked_at=timezone.now())
        SMSMessageRecipient.objects.create(campaign=c, phone='+13105550102', status='delivered',
                                           click_count=0, opted_out_at=timezone.now())

        annotated = _annotate_counts(SMSCampaign.objects.filter(id=c.id)).get()
        self.assertEqual(annotated.unique_clicks, 1)
        self.assertEqual(annotated.total_clicks, 3)
        self.assertEqual(annotated.unsub_count, 1)

        summary = MarketingAnalyticsService(org, window_days=90).calculate()['native_sms']
        self.assertEqual(summary['unique_clicks'], 1)
        self.assertEqual(summary['total_clicks'], 3)
        self.assertEqual(summary['unsubscribes'], 1)


@override_settings(E2E_TEST_MODE=True, SMS_CAMPAIGN_MAX_RECIPIENTS=5000)
class SMSCampaignLinkAutoDeriveTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Org', slug='org-ad', sms_marketing_enabled=True)
        self.user = User.objects.create_user('u', 'u@test.com', 'pw')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='u@test.com', password='pw')
        self.client.get(reverse('tickets:home'))
        make_customer(self.org, 'a@x.com', '+13105551101')
        self.rl = SMSRecipientList.objects.create(organization=self.org, name='All', filter_criteria={'min_ltv': '0'})

    def test_create_derives_link_url_from_body(self):
        self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': 'Promo', 'recipient_list': str(self.rl.id),
            'body': 'Grab tickets at https://shop.co/sale today', 'send_mode': 'now', 'confirm': '1',
        })
        c = SMSCampaign.objects.get()
        self.assertEqual(c.link_url, 'https://shop.co/sale')
