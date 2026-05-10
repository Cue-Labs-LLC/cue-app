from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import (
    Event,
    EventEmailCampaign,
    Organization,
    OrganizationMembership,
    PipedreamConnectedAccount,
    UserProfile,
    Venue,
)
from .services.mailchimp import normalize_campaign_report
from .services.mailchimp_campaign_matcher import (
    MailchimpCampaignCandidate,
    MailchimpCampaignMatcher,
    MailchimpMatchResult,
)
from .services.pipedream_connect import (
    PipedreamConnectClient,
    PipedreamConnectError,
    PipedreamMailchimpClient,
    external_user_id_for_org,
)


def _report(campaign_id='cmp_1', title='Event Campaign', send_time='2025-05-15T12:00:00+00:00'):
    return {
        'id': campaign_id,
        'campaign_title': title,
        'subject_line': 'Tonight at Venue',
        'type': 'regular',
        'emails_sent': 1000,
        'abuse_reports': 1,
        'unsubscribed': 2,
        'send_time': send_time,
        'archive_url': f'https://example.com/{campaign_id}',
        'bounces': {'hard_bounces': 3, 'soft_bounces': 4, 'syntax_errors': 1},
        'opens': {'opens_total': 500, 'unique_opens': 400, 'open_rate': 0.4},
        'clicks': {'clicks_total': 75, 'unique_clicks': 50, 'click_rate': 0.05},
        'ecommerce': {'total_orders': 6, 'total_revenue': '123.45'},
        'list_id': 'list_1',
        'list_name': 'Main List',
    }


class PipedreamMailchimpClientTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Org', slug='org')
        self.connection = PipedreamConnectedAccount.objects.create(
            organization=self.org,
            app_slug='mailchimp',
            external_user_id=external_user_id_for_org(self.org),
            account_id='apn_123',
            account_name='Main Account',
        )

    def test_list_campaign_reports_paginates_through_proxy(self):
        connect_client = MagicMock()
        connect_client.run_action.return_value = {
            'ret': {
                'results': [
                    {'campaign': {
                        'id': 'cmp_1',
                        'type': 'regular',
                        'emails_sent': 100,
                        'send_time': '2025-05-15T12:00:00+00:00',
                        'long_archive_url': 'https://example.com/cmp_1',
                        'settings': {'title': 'Campaign 1', 'subject_line': 'Subject 1'},
                        'recipients': {'list_id': 'list_1', 'list_name': 'Main List'},
                    }},
                    {'campaign': {
                        'id': 'draft_1',
                        'type': 'regular',
                        'emails_sent': 0,
                        'send_time': '',
                        'settings': {'title': 'Draft Campaign'},
                    }},
                    {'campaign': {
                        'id': 'cmp_2',
                        'type': 'regular',
                        'emails_sent': 200,
                        'send_time': '2025-05-16T12:00:00+00:00',
                        'settings': {'title': 'Campaign 2', 'subject_line': 'Subject 2'},
                    }},
                ]
            }
        }

        reports = PipedreamMailchimpClient(self.connection, connect_client).list_campaign_reports(limit=2)

        self.assertEqual([item['id'] for item in reports], ['cmp_1', 'cmp_2'])
        first_call = connect_client.run_action.call_args_list[0].args
        self.assertEqual(first_call[0], self.connection.external_user_id)
        self.assertEqual(first_call[1], 'mailchimp-search-campaign')
        self.assertEqual(first_call[2]['mailchimp']['authProvisionId'], self.connection.account_id)
        self.assertEqual(first_call[2]['query'], '*')

    def test_get_campaign_report_runs_pipedream_action(self):
        connect_client = MagicMock()
        connect_client.run_action.return_value = {'ret': _report('cmp_1', 'Campaign 1')}

        report = PipedreamMailchimpClient(self.connection, connect_client).get_campaign_report('cmp_1')

        self.assertEqual(report['id'], 'cmp_1')
        first_call = connect_client.run_action.call_args_list[0].args
        self.assertEqual(first_call[1], 'mailchimp-get-campaign-report')
        self.assertEqual(first_call[2]['campaignId'], 'cmp_1')

    @patch('tickets.services.pipedream_connect.requests.request')
    def test_pipedream_error_raises_message(self, mock_request):
        response = MagicMock(status_code=401)
        response.json.return_value = {'message': 'Invalid client'}
        mock_request.return_value = response

        with self.assertRaisesMessage(PipedreamConnectError, 'Invalid client'):
            PipedreamConnectClient().create_oauth_token()

    def test_normalize_campaign_report_summary_metrics(self):
        normalized = normalize_campaign_report(_report())

        self.assertEqual(normalized['external_id'], 'cmp_1')
        self.assertEqual(normalized['emails_sent'], 1000)
        self.assertEqual(normalized['unique_opens'], 400)
        self.assertEqual(normalized['unique_clicks'], 50)
        self.assertEqual(normalized['bounces'], 8)
        self.assertEqual(normalized['ecommerce_revenue'], Decimal('123.45'))


class MailchimpCampaignMatcherTests(TestCase):
    def test_empty_reports_short_circuits(self):
        org = Organization.objects.create(name='Org', slug='org')

        result = MailchimpCampaignMatcher(org).rank(MagicMock(), [])

        self.assertEqual(result.candidates, [])

    @patch('langchain_openai.ChatOpenAI')
    def test_rank_returns_top_ten_sorted(self, mock_chat):
        org = Organization.objects.create(name='Org', slug='org')
        venue = Venue.objects.create(organization=org, name='Room', city='Oakland')
        event = Event.objects.create(
            organization=org,
            venue=venue,
            name='Night Market',
            start_date=date(2025, 5, 20),
        )
        structured = MagicMock()
        structured.invoke.return_value = MailchimpMatchResult(candidates=[
            MailchimpCampaignCandidate(campaign_id=f'cmp_{idx}', confidence=idx / 100, reasoning='Candidate.')
            for idx in range(1, 13)
        ])
        mock_chat.return_value.with_structured_output.return_value = structured

        reports = [_report('high', 'Night Market')] * 55
        result = MailchimpCampaignMatcher(org).rank(event, reports)

        self.assertEqual(len(result.candidates), 10)
        self.assertEqual([item.campaign_id for item in result.candidates[:3]], ['cmp_12', 'cmp_11', 'cmp_10'])


@override_settings(
    PIPEDREAM_CLIENT_ID='client',
    PIPEDREAM_CLIENT_SECRET='secret',
    PIPEDREAM_PROJECT_ID='proj_123',
    PIPEDREAM_ENVIRONMENT='development',
    PIPEDREAM_MAILCHIMP_APP_SLUG='mailchimp',
)
class MailchimpViewTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Mailchimp Org', slug='mailchimp-org')
        self.user = User.objects.create_user(username='owner', email='owner@example.com', password='pass123')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(
            user=self.user,
            organization=self.org,
            org_role=UserProfile.OrgRole.OWNER,
        )
        self.venue = Venue.objects.create(organization=self.org, name='Venue', city='Oakland')
        self.event = Event.objects.create(
            organization=self.org,
            venue=self.venue,
            name='Event',
            start_date=date(2025, 6, 1),
        )
        self.connection = PipedreamConnectedAccount.objects.create(
            organization=self.org,
            app_slug='mailchimp',
            external_user_id=external_user_id_for_org(self.org),
            account_id='apn_123',
            account_name='Main Account',
        )
        self.client.force_login(self.user)
        session = self.client.session
        session['_org_id'] = str(self.org.id)
        session.save()

    @patch('tickets.views.PipedreamConnectClient')
    def test_connect_redirects_to_pipedream_connect_link(self, mock_client_cls):
        self.connection.delete()
        client = mock_client_cls.return_value
        client.create_connect_token.return_value = {'connect_link_url': 'https://pipedream.com/_static/connect.html?token=abc'}
        client.build_connect_link.return_value = 'https://pipedream.com/_static/connect.html?token=abc&connectLink=true&app=mailchimp'

        response = self.client.post(reverse('tickets:mailchimp_connect'))

        self.assertEqual(response.status_code, 302)
        self.assertIn('app=mailchimp', response.url)
        client.create_connect_token.assert_called_once()

    @patch('tickets.views.PipedreamConnectClient')
    def test_callback_stores_newest_healthy_pipedream_account(self, mock_client_cls):
        self.connection.delete()
        client = mock_client_cls.return_value
        client.list_accounts.return_value = [
            {
                'id': 'apn_old',
                'name': 'Old Account',
                'external_id': external_user_id_for_org(self.org),
                'app': {'name_slug': 'mailchimp'},
                'created_at': '2026-05-08T15:00:00+00:00',
                'healthy': True,
            },
            {
                'id': 'apn_new',
                'name': 'New Account',
                'external_id': external_user_id_for_org(self.org),
                'app': {'name_slug': 'mailchimp'},
                'created_at': '2026-05-09T15:00:00+00:00',
                'healthy': True,
            },
        ]

        response = self.client.get(reverse('tickets:mailchimp_callback'))

        self.assertEqual(response.status_code, 302)
        connection = PipedreamConnectedAccount.objects.get(organization=self.org, app_slug='mailchimp', deleted_at__isnull=True)
        self.assertEqual(connection.account_id, 'apn_new')
        self.assertEqual(connection.account_name, 'New Account')
        self.assertEqual(connection.external_user_id, external_user_id_for_org(self.org))

    @patch('tickets.views.PipedreamConnectClient')
    def test_disconnect_deletes_pipedream_account_and_soft_deletes_local_row(self, mock_client_cls):
        response = self.client.post(reverse('tickets:mailchimp_disconnect'))

        self.assertEqual(response.status_code, 302)
        self.connection.refresh_from_db()
        self.assertIsNotNone(self.connection.deleted_at)
        mock_client_cls.return_value.delete_account.assert_called_once_with('apn_123')

    @patch('tickets.views.MailchimpCampaignMatcher')
    @patch('tickets.views.PipedreamMailchimpClient')
    def test_match_endpoint_returns_modal_json(self, mock_client_cls, mock_matcher_cls):
        client = mock_client_cls.return_value
        client.list_campaign_reports.return_value = [
            _report(f'cmp_{idx}', f'Event Campaign {idx}', '2026-05-08T15:00:00+00:00')
            for idx in range(1, 11)
        ]
        mock_matcher_cls.return_value.rank.return_value = MailchimpMatchResult(candidates=[
            MailchimpCampaignCandidate(campaign_id=f'cmp_{idx}', confidence=0.84, reasoning='Name and date match.')
            for idx in range(1, 11)
        ])

        response = self.client.get(
            reverse('tickets:event_mailchimp_match', kwargs={'event_id': self.event.id}),
            {'format': 'json'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['account_name'], 'Main Account')
        self.assertEqual(len(payload['candidates']), 10)
        self.assertEqual(payload['candidates'][0]['campaign_id'], 'cmp_1')
        self.assertEqual(payload['candidates'][0]['confidence_pct'], 84)
        self.assertEqual(payload['candidates'][0]['send_time'], 'Friday, May 8th 2026 at 8:00 AM PST')

    @patch('tickets.views.PipedreamMailchimpClient')
    def test_match_endpoint_rejects_unconnected_org(self, mock_client_cls):
        self.connection.delete()

        response = self.client.get(
            reverse('tickets:event_mailchimp_match', kwargs={'event_id': self.event.id}),
            {'format': 'json'},
        )

        self.assertEqual(response.status_code, 400)
        mock_client_cls.assert_not_called()

    @patch('tickets.views.PipedreamMailchimpClient')
    def test_apply_upserts_single_mailchimp_campaign(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.list_campaign_reports.return_value = [_report('cmp_1', 'Event Campaign')]
        client.get_campaign_report.side_effect = [
            _report('cmp_1', 'Event Campaign'),
            _report('cmp_1', 'Updated Event Campaign'),
        ]
        url = reverse('tickets:event_mailchimp_apply', kwargs={'event_id': self.event.id})

        first = self.client.post(url, {'campaign_id': 'cmp_1', 'confidence': '0.84', 'reasoning': 'Name match.'})
        second = self.client.post(url, {'campaign_id': 'cmp_1', 'confidence': '0.90', 'reasoning': 'Updated match.'})

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        campaigns = EventEmailCampaign.objects.filter(event=self.event, source='mailchimp', deleted_at__isnull=True)
        self.assertEqual(campaigns.count(), 1)
        campaign = campaigns.get()
        self.assertEqual(campaign.campaign_title, 'Updated Event Campaign')
        self.assertEqual(campaign.external_id, 'cmp_1')
        self.assertEqual(campaign.emails_sent, 1000)
        self.assertEqual(campaign.unique_opens, 400)
        self.assertEqual(campaign.ecommerce_revenue, Decimal('123.45'))
        self.assertEqual(campaign.match_confidence, Decimal('0.900'))
        self.assertEqual(campaign.version, 2)

    @patch('tickets.views.PipedreamMailchimpClient')
    def test_apply_allows_multiple_linked_campaigns(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.list_campaign_reports.return_value = [
            _report('cmp_1', 'Event Campaign'),
            _report('cmp_2', 'Retargeting Campaign'),
        ]
        client.get_campaign_report.side_effect = [
            _report('cmp_1', 'Event Campaign'),
            _report('cmp_2', 'Retargeting Campaign'),
        ]
        url = reverse('tickets:event_mailchimp_apply', kwargs={'event_id': self.event.id})

        self.client.post(url, {'campaign_id': 'cmp_1'})
        self.client.post(url, {'campaign_id': 'cmp_2'})

        campaigns = EventEmailCampaign.objects.filter(event=self.event, source='mailchimp', deleted_at__isnull=True)
        self.assertEqual(campaigns.count(), 2)
        self.assertEqual(set(campaigns.values_list('external_id', flat=True)), {'cmp_1', 'cmp_2'})

    @patch('tickets.views.PipedreamMailchimpClient')
    def test_refresh_action_updates_only_selected_campaign(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.get_campaign_report.return_value = _report('cmp_1', 'Updated Campaign')
        selected = EventEmailCampaign.objects.create(
            event=self.event,
            source='mailchimp',
            external_id='cmp_1',
            campaign_title='Old Campaign',
            emails_sent=10,
        )
        other = EventEmailCampaign.objects.create(
            event=self.event,
            source='mailchimp',
            external_id='cmp_2',
            campaign_title='Other Campaign',
            emails_sent=20,
        )

        response = self.client.post(reverse('tickets:event_mailchimp_refresh', kwargs={
            'event_id': self.event.id,
            'email_campaign_id': selected.id,
        }))

        self.assertEqual(response.status_code, 302)
        self.assertIn('?tab=marketing', response.url)
        selected.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(selected.campaign_title, 'Updated Campaign')
        self.assertEqual(selected.emails_sent, 1000)
        self.assertEqual(selected.version, 2)
        self.assertEqual(other.emails_sent, 20)
        self.assertEqual(other.version, 1)

    @patch('tickets.views.PipedreamMailchimpClient')
    def test_bulk_refresh_updates_linked_campaigns_for_marketing_tab(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.get_campaign_report.side_effect = [
            _report('cmp_1', 'Updated Campaign'),
            _report('cmp_2', 'Updated Retargeting'),
        ]
        EventEmailCampaign.objects.create(
            event=self.event,
            source='mailchimp',
            external_id='cmp_1',
            campaign_title='Old Campaign',
            emails_sent=10,
        )
        EventEmailCampaign.objects.create(
            event=self.event,
            source='mailchimp',
            external_id='cmp_2',
            campaign_title='Old Retargeting',
            emails_sent=20,
        )

        response = self.client.post(reverse('tickets:event_mailchimp_refresh_all', kwargs={
            'event_id': self.event.id,
        }), HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(len(payload['campaigns']), 2)
        self.assertEqual(
            set(EventEmailCampaign.objects.filter(event=self.event).values_list('campaign_title', flat=True)),
            {'Updated Campaign', 'Updated Retargeting'},
        )
        self.assertEqual(client.get_campaign_report.call_count, 2)

    @patch('tickets.views.PipedreamMailchimpClient')
    def test_refresh_failure_keeps_existing_metrics(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.get_campaign_report.side_effect = PipedreamConnectError('Mailchimp unavailable')
        campaign = EventEmailCampaign.objects.create(
            event=self.event,
            source='mailchimp',
            external_id='cmp_1',
            campaign_title='Old Campaign',
            emails_sent=10,
        )

        response = self.client.post(reverse('tickets:event_mailchimp_refresh', kwargs={
            'event_id': self.event.id,
            'email_campaign_id': campaign.id,
        }))

        self.assertEqual(response.status_code, 302)
        campaign.refresh_from_db()
        self.assertEqual(campaign.emails_sent, 10)
        self.assertEqual(campaign.version, 1)

    def test_remove_action_soft_deletes_only_selected_campaign(self):
        selected = EventEmailCampaign.objects.create(
            event=self.event,
            source='mailchimp',
            external_id='cmp_1',
            campaign_title='Selected Campaign',
        )
        other = EventEmailCampaign.objects.create(
            event=self.event,
            source='mailchimp',
            external_id='cmp_2',
            campaign_title='Other Campaign',
        )

        response = self.client.post(reverse('tickets:event_mailchimp_remove', kwargs={
            'event_id': self.event.id,
            'email_campaign_id': selected.id,
        }))

        self.assertEqual(response.status_code, 302)
        selected.refresh_from_db()
        other.refresh_from_db()
        self.assertIsNotNone(selected.deleted_at)
        self.assertIsNone(other.deleted_at)

    def test_mailchimp_row_actions_404_for_wrong_event_campaign(self):
        other_org = Organization.objects.create(name='Other Org', slug='other-org')
        other_venue = Venue.objects.create(organization=other_org, name='Other Venue', city='Oakland')
        other_event = Event.objects.create(
            organization=other_org,
            venue=other_venue,
            name='Other Event',
            start_date=date(2025, 6, 2),
        )
        other_campaign = EventEmailCampaign.objects.create(
            event=other_event,
            source='mailchimp',
            external_id='cmp_other',
            campaign_title='Other Campaign',
        )

        refresh_other = self.client.post(reverse('tickets:event_mailchimp_refresh', kwargs={
            'event_id': self.event.id,
            'email_campaign_id': other_campaign.id,
        }))
        remove_other = self.client.post(reverse('tickets:event_mailchimp_remove', kwargs={
            'event_id': self.event.id,
            'email_campaign_id': other_campaign.id,
        }))

        self.assertEqual(refresh_other.status_code, 404)
        self.assertEqual(remove_other.status_code, 404)

    def test_event_detail_lists_linked_mailchimp_campaigns(self):
        EventEmailCampaign.objects.create(
            event=self.event,
            source='mailchimp',
            external_id='cmp_1',
            campaign_title='Event Campaign',
            subject_line='Tonight at Venue',
            send_time=datetime(2026, 5, 8, 15, 0, tzinfo=timezone.utc),
            emails_sent=1000,
            unique_opens=400,
            unique_clicks=50,
            ecommerce_revenue=Decimal('123.45'),
        )

        response = self.client.get(reverse('tickets:event_detail', kwargs={'event_id': self.event.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Mailchimp')
        self.assertContains(response, 'data-bs-target="#mailchimpMatchModal"')
        self.assertContains(response, 'data-mailchimp-refresh-url=')
        self.assertContains(response, 'id="mailchimpCampaignRows"')
        self.assertContains(response, 'Event Campaign')
        self.assertContains(response, 'Tonight at Venue')
        self.assertContains(response, '1000')
        self.assertContains(response, '$123.45')
