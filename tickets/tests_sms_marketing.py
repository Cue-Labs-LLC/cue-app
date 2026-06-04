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
        self.org = Organization.objects.create(name='Org A', slug='org-va', sms_marketing_enabled=True, sms_credit_balance_cents=100000)
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
        # Consolidated SMS home: shared Marketing nav + performance band + table.
        self.assertContains(resp, 'marketing-sectionnav')
        self.assertContains(resp, 'Campaigns sent')   # native performance stat card
        self.assertContains(resp, 'Your campaigns')    # campaign table section

    def test_marketing_overview_has_section_nav_and_no_sms_tab(self):
        # SMS Campaigns now lives in the Marketing page's primary nav; the old
        # in-page "SMS" analytics tab is gone (its metrics moved to the SMS page).
        resp = self.client.get(reverse('tickets:marketing_overview'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'marketing-sectionnav')
        self.assertContains(resp, reverse('tickets:sms_campaign_list'))
        self.assertNotContains(resp, 'data-tab-key="sms"')

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
        # Send-now dispatches via transaction.on_commit — capture so it fires in TestCase.
        with self.captureOnCommitCallbacks(execute=True):
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
        self.org = Organization.objects.create(name='Org', slug='org-ad', sms_marketing_enabled=True, sms_credit_balance_cents=100000)
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


@override_settings(E2E_TEST_MODE=True, SMS_CAMPAIGN_MAX_RECIPIENTS=5000)
class EventSMSTests(TestCase):
    """Sending native marketing SMS from an event's Marketing tab."""
    def setUp(self):
        from datetime import date
        from .models import Event, Venue, CSVFormat, UploadedFile, TicketOrder
        self.TicketOrder = TicketOrder
        self.client = Client()
        self.org = Organization.objects.create(name='Org', slug='org-ev', sms_marketing_enabled=True, sms_credit_balance_cents=100000)
        self.user = User.objects.create_user('u', 'u@test.com', 'pw')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='u@test.com', password='pw')
        self.client.get(reverse('tickets:home'))
        self.venue = Venue.objects.create(organization=self.org, name='V', city='LA')
        self.event = Event.objects.create(
            organization=self.org, name='Summer Fest', venue=self.venue, start_date=date(2026, 7, 1),
        )
        self.fmt = CSVFormat.objects.create(organization=self.org, name='F', column_mapping={'order_number': 'O'})
        self.upload = UploadedFile.objects.create(
            organization=self.org, csv_format=self.fmt, filename='f.csv', status='completed',
        )
        # Two opted-in attendees, one opted-out attendee, one opted-in non-attendee.
        self.a1 = make_customer(self.org, 'a1@x.com', '+13105550001')
        self.a2 = make_customer(self.org, 'a2@x.com', '+13105550002')
        self.optout = make_customer(self.org, 'o@x.com', '+13105550003', opt_in=False)
        self.nonatt = make_customer(self.org, 'n@x.com', '+13105550004')
        for c in (self.a1, self.a2, self.optout):
            TicketOrder.objects.create(
                customer=c, event=self.event, uploaded_file=self.upload,
                order_number=f'O-{c.email}', order_date=timezone.now(), total_amount=Decimal('50.00'),
            )

    def test_event_id_filter_returns_all_buyers(self):
        from .services.customer_filters import filter_customers
        emails = set(
            filter_customers(self.org, {'event_id': str(self.event.id)}).distinct()
            .values_list('email', flat=True)
        )
        self.assertEqual(emails, {'a1@x.com', 'a2@x.com', 'o@x.com'})  # excludes non-attendee

    def test_attendee_list_resolves_opted_in_only(self):
        from .sms_views import _event_attendee_list
        lst = _event_attendee_list(self.org, self.event)
        phones = {r['phone'] for r in lst.materialize(self.org)}
        self.assertEqual(phones, {'+13105550001', '+13105550002'})  # opt-out + non-attendee excluded

    def test_attendee_list_idempotent(self):
        from .sms_views import _event_attendee_list
        a = _event_attendee_list(self.org, self.event)
        b = _event_attendee_list(self.org, self.event)
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(
            SMSRecipientList.objects.filter(
                organization=self.org, filter_criteria={'event_id': str(self.event.id)},
            ).count(), 1,
        )

    def test_create_event_mode_get_seeds_form(self):
        resp = self.client.get(reverse('tickets:sms_campaign_create'), {'event': str(self.event.id)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['event'], self.event)
        form = resp.context['form']
        self.assertEqual(form.initial.get('name'), self.event.name)
        self.assertIsNotNone(form.initial.get('recipient_list'))

    def test_create_event_mode_sends_to_attendees_and_links_event(self):
        from .sms_views import _event_attendee_list
        lst = _event_attendee_list(self.org, self.event)
        resp = self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': self.event.name, 'recipient_list': str(lst.id), 'body': 'See you there!',
            'send_mode': 'now', 'event': str(self.event.id), 'confirm': '1',
        })
        self.assertEqual(resp.status_code, 302)
        c = SMSCampaign.objects.get()
        self.assertEqual(c.event_id, self.event.id)
        phones = set(SMSMessageRecipient.objects.filter(campaign=c).values_list('phone', flat=True))
        self.assertEqual(phones, {'+13105550001', '+13105550002'})

    def test_cross_tenant_event_404(self):
        from datetime import date
        from .models import Event, Venue
        other = Organization.objects.create(name='B', slug='org-evb', sms_marketing_enabled=True)
        ov = Venue.objects.create(organization=other, name='V', city='SF')
        oe = Event.objects.create(organization=other, name='Theirs', venue=ov, start_date=date(2026, 7, 1))
        resp = self.client.get(reverse('tickets:sms_campaign_create'), {'event': str(oe.id)})
        self.assertEqual(resp.status_code, 404)

    def test_marketing_tab_button_gated_by_flag(self):
        resp = self.client.get(reverse('tickets:event_detail', args=[self.event.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Send SMS to attendees')
        self.org.sms_marketing_enabled = False
        self.org.save(update_fields=['sms_marketing_enabled'])
        resp = self.client.get(reverse('tickets:event_detail', args=[self.event.id]))
        self.assertNotContains(resp, 'Send SMS to attendees')


@override_settings(E2E_TEST_MODE=True, SMS_CAMPAIGN_MAX_RECIPIENTS=5000,
                   SMS_PRICE_PER_SEGMENT_CENTS=Decimal('3'))
class SMSCreditWalletTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Org', slug='org-cw', sms_marketing_enabled=True)

    def test_cost_estimate_segments(self):
        from .services.sms_credits import estimate_campaign_cost_cents
        # 2 recipients, short body -> 1 segment, 3c each = 6c
        self.assertEqual(estimate_campaign_cost_cents(2, 'hi'), 6)
        self.assertEqual(estimate_campaign_cost_cents(0, 'hi'), 0)

    def test_charge_debits_and_records_ledger(self):
        from .services.sms_credits import charge
        from .models import SMSCreditTransaction
        self.org.sms_credit_balance_cents = 100
        self.org.save(update_fields=['sms_credit_balance_cents'])
        new_balance = charge(self.org.id, 30, description='test')
        self.assertEqual(new_balance, 70)
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 70)
        t = SMSCreditTransaction.objects.get(organization=self.org, kind='charge')
        self.assertEqual(t.amount_cents, -30)
        self.assertEqual(t.balance_after_cents, 70)

    def test_charge_insufficient_raises_and_no_mutation(self):
        from .services.sms_credits import charge, InsufficientCreditsError
        from .models import SMSCreditTransaction
        self.org.sms_credit_balance_cents = 10
        self.org.save(update_fields=['sms_credit_balance_cents'])
        with self.assertRaises(InsufficientCreditsError):
            charge(self.org.id, 50)
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 10)
        self.assertFalse(SMSCreditTransaction.objects.filter(organization=self.org).exists())

    def test_credit_idempotent_by_session(self):
        from .services.sms_credits import credit
        from .models import SMSCreditTransaction
        b1 = credit(self.org.id, 1000, stripe_checkout_session_id='cs_test_1', description='topup')
        b2 = credit(self.org.id, 1000, stripe_checkout_session_id='cs_test_1', description='topup retry')
        self.assertEqual(b1, 1000)
        self.assertEqual(b2, 1000)  # retry no-ops
        self.assertEqual(SMSCreditTransaction.objects.filter(organization=self.org, kind='topup').count(), 1)

    def test_refund_campaign_is_idempotent(self):
        from .services.sms_credits import charge, refund_campaign
        rl = SMSRecipientList.objects.create(organization=self.org, name='L', filter_criteria={'min_ltv': '0'})
        c = SMSCampaign.objects.create(organization=self.org, recipient_list=rl, name='C', body='Hi',
                                       status=SMSCampaign.Status.SCHEDULED)
        self.org.sms_credit_balance_cents = 100
        self.org.save(update_fields=['sms_credit_balance_cents'])
        charge(self.org.id, 30, campaign=c, description='charge')
        refunded = refund_campaign(c)
        self.assertEqual(refunded, 30)
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 100)  # back to full
        # Second refund is a no-op.
        self.assertEqual(refund_campaign(c), 0)
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 100)

    def test_webhook_credits_idempotently(self):
        from .views import _fulfill_sms_credit_checkout
        from .models import SMSCreditTransaction
        session = {
            'id': 'cs_hook_1', 'payment_status': 'paid', 'amount_total': 2500,
            'metadata': {'kind': 'sms_credits', 'organization_id': str(self.org.id), 'credit_cents': '2500'},
        }
        _fulfill_sms_credit_checkout(session)
        _fulfill_sms_credit_checkout(session)  # retry
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 2500)
        self.assertEqual(SMSCreditTransaction.objects.filter(stripe_checkout_session_id='cs_hook_1').count(), 1)

    def test_webhook_credits_from_stripe_object(self):
        # Regression: the live webhook passes a Stripe StripeObject, not a dict.
        # StripeObject has no .get() (its __getattr__ raises on a missing key),
        # so `session.get('metadata')` 500'd in prod while dict-based tests passed.
        from stripe import StripeObject
        from .views import _fulfill_sms_credit_checkout
        from .models import SMSCreditTransaction
        session = StripeObject.construct_from({
            'id': 'cs_obj_1', 'payment_status': 'paid', 'amount_total': 1500,
            'metadata': {'kind': 'sms_credits', 'organization_id': str(self.org.id), 'credit_cents': '1500'},
        }, 'sk_test')
        _fulfill_sms_credit_checkout(session)
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 1500)
        self.assertEqual(SMSCreditTransaction.objects.filter(stripe_checkout_session_id='cs_obj_1').count(), 1)

    def test_webhook_ignores_non_sms_sessions(self):
        from .views import _fulfill_sms_credit_checkout
        _fulfill_sms_credit_checkout({'id': 'cs_x', 'payment_status': 'paid', 'metadata': {'kind': 'something_else'}})
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 0)


@override_settings(E2E_TEST_MODE=True, SMS_CAMPAIGN_MAX_RECIPIENTS=5000,
                   SMS_PRICE_PER_SEGMENT_CENTS=Decimal('3'))
class SMSCreditSendFlowTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Org', slug='org-cs', sms_marketing_enabled=True)
        self.user = User.objects.create_user('u', 'u@test.com', 'pw')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='u@test.com', password='pw')
        self.client.get(reverse('tickets:home'))
        for i in range(2):
            make_customer(self.org, f'c{i}@x.com', f'+1310555900{i}')
        self.rl = SMSRecipientList.objects.create(organization=self.org, name='All', filter_criteria={'min_ltv': '0'})

    def _post_send(self, **extra):
        data = {
            'name': 'Promo', 'recipient_list': str(self.rl.id), 'body': 'Hello',
            'send_mode': 'now', 'confirm': '1',
        }
        data.update(extra)
        # Send-now dispatches via transaction.on_commit; capture so it fires in TestCase.
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse('tickets:sms_campaign_create'), data)

    def test_insufficient_balance_blocks_send(self):
        # Balance 0; cost = 2 recipients x 1 segment x 3c = 6c.
        resp = self._post_send()
        self.assertEqual(resp.status_code, 200)  # not redirected
        self.assertTrue(resp.context['insufficient_credits'])
        self.assertEqual(SMSCampaign.objects.count(), 0)  # nothing created

    def test_sufficient_balance_charges_and_sends(self):
        self.org.sms_credit_balance_cents = 100
        self.org.save(update_fields=['sms_credit_balance_cents'])
        resp = self._post_send()
        self.assertEqual(resp.status_code, 302)
        c = SMSCampaign.objects.get()
        self.assertEqual(c.status, SMSCampaign.Status.SENT)
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 94)  # 100 - 6

    def test_confirm_step_shows_cost(self):
        self.org.sms_credit_balance_cents = 100
        self.org.save(update_fields=['sms_credit_balance_cents'])
        # POST without confirm -> shows cost, no send.
        resp = self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': 'Promo', 'recipient_list': str(self.rl.id), 'body': 'Hello', 'send_mode': 'now',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['confirm_cost_cents'], 6)
        self.assertEqual(resp.context['confirm_cost_tokens'], 2)  # 2 recipients x 1 segment
        self.assertFalse(resp.context['insufficient_credits'])
        self.assertEqual(SMSCampaign.objects.count(), 0)

    def test_cancel_scheduled_refunds(self):
        self.org.sms_credit_balance_cents = 100
        self.org.save(update_fields=['sms_credit_balance_cents'])
        when = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': 'Later', 'recipient_list': str(self.rl.id), 'body': 'Hello',
            'send_mode': 'schedule', 'scheduled_at': when, 'confirm': '1',
        })
        c = SMSCampaign.objects.get()
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 94)  # charged at schedule time
        self.client.post(reverse('tickets:sms_campaign_cancel', kwargs={'pk': c.id}))
        c.refresh_from_db()
        self.assertEqual(c.status, SMSCampaign.Status.CANCELED)
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 100)  # refunded

    def test_credits_page_renders_token_balance(self):
        # 4200 cents at 3c/token = 1400 tokens.
        self.org.sms_credit_balance_cents = 4200
        self.org.save(update_fields=['sms_credit_balance_cents'])
        resp = self.client.get(reverse('tickets:sms_credits'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '1400')
        self.assertContains(resp, 'Token balance')
        self.assertNotContains(resp, '$42.00')


@override_settings(E2E_TEST_MODE=True, SMS_CAMPAIGN_MAX_RECIPIENTS=5000,
                   SMS_PRICE_PER_SEGMENT_CENTS=Decimal('3'))
class SMSCreditHardeningTests(TestCase):
    """Covers the eng-review + outside-voice fixes: idempotent confirm, frozen
    audience + opt-out re-check, recoverable send, ceil rounding, settled-amount
    crediting, webhook payment guards, and checkout validation."""
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Org', slug='org-hd',
                                                sms_marketing_enabled=True, sms_credit_balance_cents=100000)
        self.user = User.objects.create_user('u', 'u@test.com', 'pw')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='u@test.com', password='pw')
        self.client.get(reverse('tickets:home'))
        for i in range(2):
            make_customer(self.org, f'c{i}@x.com', f'+1310555800{i}')
        self.rl = SMSRecipientList.objects.create(organization=self.org, name='All', filter_criteria={'min_ltv': '0'})

    def _confirm(self, key, **extra):
        data = {'name': 'Promo', 'recipient_list': str(self.rl.id), 'body': 'Hello',
                'send_mode': 'now', 'confirm': '1', 'idempotency_key': key}
        data.update(extra)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse('tickets:sms_campaign_create'), data)

    def test_h2_duplicate_confirm_is_idempotent(self):
        # Two submits with the same idempotency key -> ONE campaign, charged once.
        from .models import SMSCreditTransaction
        self._confirm('dup-key-1')
        self._confirm('dup-key-1')  # double-click
        self.assertEqual(SMSCampaign.objects.filter(organization=self.org).count(), 1)
        charges = SMSCreditTransaction.objects.filter(organization=self.org, kind='charge')
        self.assertEqual(charges.count(), 1)

    def test_h1_send_now_is_recoverable_via_cron(self):
        # Without firing on_commit, the campaign is left scheduled-for-now so the
        # cron safety net still sends it (no lost money / silent drop).
        resp = self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': 'P', 'recipient_list': str(self.rl.id), 'body': 'Hi',
            'send_mode': 'now', 'confirm': '1', 'idempotency_key': 'recov-1',
        })
        self.assertEqual(resp.status_code, 302)
        c = SMSCampaign.objects.get()
        self.assertEqual(c.status, SMSCampaign.Status.SCHEDULED)
        self.assertLessEqual(c.scheduled_at, timezone.now())
        # The */5 cron picks it up and sends it.
        call_command('send_due_sms_campaigns')
        c.refresh_from_db()
        self.assertEqual(c.status, SMSCampaign.Status.SENT)

    def test_frozen_audience_snapshot_at_confirm(self):
        # Recipients are frozen at confirm time (charged == snapshot), even before send.
        self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': 'P', 'recipient_list': str(self.rl.id), 'body': 'Hi',
            'send_mode': 'now', 'confirm': '1', 'idempotency_key': 'frz-1',
        })
        c = SMSCampaign.objects.get()
        self.assertEqual(c.audience_size, 2)
        self.assertEqual(SMSMessageRecipient.objects.filter(campaign=c).count(), 2)

    def test_opt_out_after_freeze_is_skipped_at_send(self):
        # Freeze the audience, then a recipient opts out before the send fires.
        self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': 'P', 'recipient_list': str(self.rl.id), 'body': 'Hi',
            'send_mode': 'now', 'confirm': '1', 'idempotency_key': 'opt-1',
        })
        c = SMSCampaign.objects.get()
        PhoneSuppression.objects.create(phone='+13105558000', organization=None)
        from .tasks import send_sms_campaign_task
        send_sms_campaign_task.delay(str(c.id))
        sent = set(SMSMessageRecipient.objects.filter(campaign=c, status='sent').values_list('phone', flat=True))
        skipped = SMSMessageRecipient.objects.filter(campaign=c, status='failed', phone='+13105558000').first()
        self.assertNotIn('+13105558000', sent)        # opted-out not texted
        self.assertIsNotNone(skipped)
        self.assertEqual(skipped.error_message, 'opted out before send')

    @override_settings(SMS_PRICE_PER_SEGMENT_CENTS=Decimal('0.4'))
    def test_m4_sub_cent_rounds_up_not_free(self):
        from .services.sms_credits import estimate_campaign_cost_cents
        # 1 recipient x 1 segment x 0.4c = 0.4c -> ceil -> 1c (never free)
        self.assertEqual(estimate_campaign_cost_cents(1, 'hi'), 1)

    def test_m5_credits_settled_amount_not_metadata(self):
        # If Stripe's settled amount differs from metadata, credit the settled amount.
        from .views import _fulfill_sms_credit_checkout
        start = self.org.sms_credit_balance_cents
        _fulfill_sms_credit_checkout({
            'id': 'cs_settled_1', 'payment_status': 'paid', 'amount_total': 1800,
            'metadata': {'kind': 'sms_credits', 'organization_id': str(self.org.id), 'credit_cents': '2500'},
        })
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, start + 1800)  # settled, not 2500

    def test_webhook_unpaid_does_not_credit(self):
        from .views import _fulfill_sms_credit_checkout
        start = self.org.sms_credit_balance_cents
        _fulfill_sms_credit_checkout({
            'id': 'cs_unpaid', 'payment_status': 'unpaid', 'amount_total': 1000,
            'metadata': {'kind': 'sms_credits', 'organization_id': str(self.org.id)},
        })
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, start)

    def test_webhook_propagates_on_credit_failure(self):
        # Fix #3: the webhook must NOT swallow — a credit failure raises so Stripe retries.
        from .views import _fulfill_sms_credit_checkout
        with patch('tickets.services.sms_credits.credit', side_effect=RuntimeError('db down')):
            with self.assertRaises(RuntimeError):
                _fulfill_sms_credit_checkout({
                    'id': 'cs_fail', 'payment_status': 'paid', 'amount_total': 1000,
                    'metadata': {'kind': 'sms_credits', 'organization_id': str(self.org.id)},
                })

    def test_checkout_rejects_invalid_token_pack(self):
        resp = self.client.post(reverse('tickets:sms_credits_checkout'), {'tokens': '777'})
        self.assertEqual(resp.status_code, 302)  # redirect back, no Stripe call
        self.assertEqual(resp['Location'], reverse('tickets:sms_credits'))

    @override_settings(STRIPE_SECRET_KEY='sk_test_x')
    def test_checkout_charges_token_pack_at_price(self):
        # A 500-token pack at 3c = $15.00; Stripe unit_amount must be 1500 cents.
        with patch('stripe.checkout.Session.create') as mock_create:
            mock_create.return_value = type('S', (), {'url': 'https://stripe.test/cs'})()
            resp = self.client.post(reverse('tickets:sms_credits_checkout'), {'tokens': '500'})
        self.assertEqual(resp.status_code, 302)
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs['line_items'][0]['price_data']['unit_amount'], 1500)
        self.assertEqual(kwargs['metadata']['credit_cents'], '1500')


@override_settings(E2E_TEST_MODE=True, SMS_PRICE_PER_SEGMENT_CENTS=Decimal('3'))
class SMSTokenFilterTests(TestCase):
    def test_tokens_filter_floor_and_signs(self):
        from .templatetags.tickets_extras import tokens
        self.assertEqual(tokens(4200), 1400)   # 4200 / 3
        self.assertEqual(tokens(1000), 333)    # floor, odd cents (admin grant)
        self.assertEqual(tokens(-6), -2)       # whole-token debit
        self.assertEqual(tokens(-1), -1)       # floor, not int()-truncate toward 0
        self.assertEqual(tokens(0), 0)
        self.assertEqual(tokens(None), 0)

    def test_cost_tokens_is_exact_segment_count(self):
        from .services.sms_credits import estimate_campaign_cost_tokens
        # 3 recipients, short body -> 1 segment each = 3 tokens (not cents/price).
        self.assertEqual(estimate_campaign_cost_tokens(3, 'hi'), 3)
        self.assertEqual(estimate_campaign_cost_tokens(0, 'hi'), 0)


@override_settings(E2E_TEST_MODE=True, SMS_PRICE_PER_SEGMENT_CENTS=Decimal('3'),
                   STRIPE_SECRET_KEY='sk_test_x', STRIPE_WEBHOOK_SECRET='whsec_x')
class SMSSavedCardTests(TestCase):
    """Card-on-file: save during top-up, one-click off-session reuse, remove."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Org SC', slug='org-sc', sms_marketing_enabled=True)
        self.user = User.objects.create_user('usc', 'usc@test.com', 'pw')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='usc@test.com', password='pw')
        self.client.get(reverse('tickets:home'))

    def _set_saved_card(self):
        Organization.objects.filter(pk=self.org.pk).update(
            stripe_customer_id='cus_1', stripe_pm_id='pm_1',
            stripe_pm_brand='visa', stripe_pm_last4='4242',
        )

    # --- save-card Checkout -------------------------------------------------
    def test_checkout_save_card_attaches_customer(self):
        with patch('stripe.Customer.create') as mock_cust, \
             patch('stripe.checkout.Session.create') as mock_sess:
            mock_cust.return_value = type('C', (), {'id': 'cus_new'})()
            mock_sess.return_value = type('S', (), {'url': 'https://stripe.test/cs'})()
            resp = self.client.post(reverse('tickets:sms_credits_checkout'),
                                    {'tokens': '500', 'save_card': '1'})
        self.assertEqual(resp.status_code, 302)
        kwargs = mock_sess.call_args.kwargs
        self.assertEqual(kwargs['customer'], 'cus_new')
        self.assertEqual(kwargs['payment_intent_data']['setup_future_usage'], 'off_session')
        self.assertEqual(kwargs['payment_intent_data']['metadata']['flow'], 'checkout')
        self.org.refresh_from_db()
        self.assertEqual(self.org.stripe_customer_id, 'cus_new')

    def test_checkout_without_save_card_unchanged(self):
        with patch('stripe.checkout.Session.create') as mock_sess:
            mock_sess.return_value = type('S', (), {'url': 'https://stripe.test/cs'})()
            self.client.post(reverse('tickets:sms_credits_checkout'), {'tokens': '500'})
        kwargs = mock_sess.call_args.kwargs
        self.assertNotIn('customer', kwargs)
        self.assertNotIn('payment_intent_data', kwargs)

    # --- one-click off-session charge --------------------------------------
    def test_charge_saved_credits_once_and_idempotent(self):
        from .models import SMSCreditTransaction
        from .views import _fulfill_sms_credit_payment_intent
        self._set_saved_card()
        with patch('stripe.PaymentIntent.create') as mock_pi:
            mock_pi.return_value = type('PI', (), {'status': 'succeeded', 'id': 'pi_oc1',
                                                   'amount_received': 1500})()
            resp = self.client.post(reverse('tickets:sms_credits_charge_saved'), {'tokens': '500'})
        self.assertEqual(resp.status_code, 302)
        kwargs = mock_pi.call_args.kwargs
        self.assertTrue(kwargs['off_session'] and kwargs['confirm'])
        self.assertEqual(kwargs['metadata']['flow'], 'one_click')
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 1500)
        # The webhook firing for the same PI must not double-credit.
        _fulfill_sms_credit_payment_intent({
            'id': 'pi_oc1', 'amount_received': 1500, 'payment_method': None,
            'metadata': {'kind': 'sms_credits', 'flow': 'one_click',
                         'organization_id': str(self.org.id), 'credit_cents': '1500'},
        })
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 1500)
        self.assertEqual(SMSCreditTransaction.objects.filter(stripe_checkout_session_id='pi_oc1').count(), 1)

    def test_charge_saved_requires_saved_card(self):
        resp = self.client.post(reverse('tickets:sms_credits_charge_saved'), {'tokens': '500'})
        self.assertEqual(resp.status_code, 302)
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 0)

    def test_charge_saved_declined_falls_back(self):
        import stripe as stripe_lib
        self._set_saved_card()
        err = stripe_lib.error.CardError('declined', None, 'card_declined')
        with patch('stripe.PaymentIntent.create', side_effect=err):
            resp = self.client.post(reverse('tickets:sms_credits_charge_saved'), {'tokens': '500'})
        self.assertEqual(resp.status_code, 302)
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 0)

    def test_charge_saved_authentication_required_falls_back(self):
        import stripe as stripe_lib
        self._set_saved_card()
        err = stripe_lib.error.CardError('auth', None, 'authentication_required')
        with patch('stripe.PaymentIntent.create', side_effect=err):
            resp = self.client.post(reverse('tickets:sms_credits_charge_saved'), {'tokens': '500'})
        self.assertEqual(resp.status_code, 302)
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 0)

    def test_charge_saved_resource_missing_clears_card(self):
        import stripe as stripe_lib
        self._set_saved_card()
        err = stripe_lib.error.InvalidRequestError('missing', 'payment_method', code='resource_missing')
        with patch('stripe.PaymentIntent.create', side_effect=err):
            self.client.post(reverse('tickets:sms_credits_charge_saved'), {'tokens': '500'})
        self.org.refresh_from_db()
        self.assertFalse(self.org.stripe_pm_id)
        self.assertEqual(self.org.stripe_pm_brand, '')
        self.assertEqual(self.org.stripe_customer_id, 'cus_1')  # customer kept

    # --- remove card -------------------------------------------------------
    def test_remove_card_detaches_and_clears(self):
        self._set_saved_card()
        with patch('stripe.PaymentMethod.detach') as mock_detach:
            self.client.post(reverse('tickets:sms_credits_remove_card'))
        mock_detach.assert_called_once_with('pm_1')
        self.org.refresh_from_db()
        self.assertFalse(self.org.stripe_pm_id)
        self.assertEqual(self.org.stripe_pm_brand, '')
        self.assertEqual(self.org.stripe_customer_id, 'cus_1')  # kept for next card

    def test_remove_card_clears_even_when_detach_missing(self):
        import stripe as stripe_lib
        self._set_saved_card()
        with patch('stripe.PaymentMethod.detach',
                   side_effect=stripe_lib.error.InvalidRequestError('gone', 'payment_method', code='resource_missing')):
            self.client.post(reverse('tickets:sms_credits_remove_card'))
        self.org.refresh_from_db()
        self.assertFalse(self.org.stripe_pm_id)

    # --- webhook PI handler ------------------------------------------------
    def test_pi_handler_checkout_flow_saves_card_no_double_credit(self):
        from .views import _fulfill_sms_credit_payment_intent
        card = type('Card', (), {'brand': 'visa', 'last4': '4242', 'exp_month': 12, 'exp_year': 2030})()
        pm = type('PM', (), {'card': card})()
        with patch('stripe.PaymentMethod.retrieve', return_value=pm):
            _fulfill_sms_credit_payment_intent({
                'id': 'pi_co', 'amount_received': 1500, 'payment_method': 'pm_2',
                'metadata': {'kind': 'sms_credits', 'flow': 'checkout',
                             'organization_id': str(self.org.id), 'credit_cents': '1500'},
            })
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 0)  # checkout flow credits via session, not here
        self.assertEqual(self.org.stripe_pm_id, 'pm_2')
        self.assertEqual(self.org.stripe_pm_brand, 'visa')
        self.assertEqual(self.org.stripe_pm_last4, '4242')

    def test_webhook_routes_sms_credit_pi_to_credit(self):
        event = {'type': 'payment_intent.succeeded', 'data': {'object': {
            'id': 'pi_route', 'amount_received': 1500, 'payment_method': None,
            'metadata': {'kind': 'sms_credits', 'flow': 'one_click',
                         'organization_id': str(self.org.id), 'credit_cents': '1500'},
        }}}
        with patch('stripe.Webhook.construct_event', return_value=event), \
             patch('tickets.views._fulfill_payment_intent') as mock_ticket:
            resp = self.client.post(reverse('tickets:stripe_webhook'), data='{}',
                                    content_type='application/json', HTTP_STRIPE_SIGNATURE='sig')
        self.assertEqual(resp.status_code, 200)
        mock_ticket.assert_not_called()  # routed to wallet handler, not ticketing
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 1500)
