from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlparse
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.cache import cache as django_cache
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import (
    Event,
    EventEmailCampaign,
    Organization,
    OrganizationMembership,
    UserProfile,
    Venue,
)
from .services.mailchimp import (
    MailchimpAPIError,
    MailchimpClient,
    build_authorize_url,
    exchange_code_for_token,
    fetch_org_reports_cached,
    get_oauth_metadata,
    normalize_campaign_report,
)
from .services.mailchimp_campaign_matcher import (
    MailchimpCampaignCandidate,
    MailchimpCampaignMatcher,
    MailchimpMatchResult,
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


def _response(status_code=200, payload=None):
    response = MagicMock(status_code=status_code)
    response.json.return_value = payload if payload is not None else {}
    return response


@override_settings(MAILCHIMP_CLIENT_ID='client', MAILCHIMP_CLIENT_SECRET='secret')
class MailchimpClientTests(TestCase):
    def test_build_authorize_url_includes_oauth_params(self):
        url = build_authorize_url('https://cue.test/settings/mailchimp/callback/', 'state_123')
        parsed = urlparse(url)
        params = parse_qs(parsed.query)

        self.assertEqual(parsed.scheme, 'https')
        self.assertEqual(parsed.netloc, 'login.mailchimp.com')
        self.assertEqual(params['response_type'], ['code'])
        self.assertEqual(params['client_id'], ['client'])
        self.assertEqual(params['redirect_uri'], ['https://cue.test/settings/mailchimp/callback/'])
        self.assertEqual(params['state'], ['state_123'])

    @patch('tickets.services.mailchimp.requests.request')
    def test_exchange_code_for_token_posts_oauth_form_fields(self, mock_request):
        mock_request.return_value = _response(payload={'access_token': 'token_123'})

        token = exchange_code_for_token('code_123', 'https://cue.test/settings/mailchimp/callback/')

        self.assertEqual(token, 'token_123')
        self.assertEqual(mock_request.call_args.args[:2], ('POST', 'https://login.mailchimp.com/oauth2/token'))
        self.assertEqual(mock_request.call_args.kwargs['data']['client_id'], 'client')
        self.assertEqual(mock_request.call_args.kwargs['data']['client_secret'], 'secret')
        self.assertEqual(mock_request.call_args.kwargs['data']['code'], 'code_123')

    @patch('tickets.services.mailchimp.requests.request')
    def test_get_oauth_metadata_uses_oauth_header(self, mock_request):
        mock_request.return_value = _response(payload={'dc': 'us18'})

        metadata = get_oauth_metadata('token_123')

        self.assertEqual(metadata['dc'], 'us18')
        self.assertEqual(mock_request.call_args.args[:2], ('GET', 'https://login.mailchimp.com/oauth2/metadata'))
        self.assertEqual(mock_request.call_args.kwargs['headers']['Authorization'], 'OAuth token_123')

    @patch('tickets.services.mailchimp.requests.request')
    def test_client_uses_bearer_auth_and_requests_full_page(self, mock_request):
        single_page = [_report(f'cmp_{idx}', f'Campaign {idx}') for idx in range(150)]
        mock_request.return_value = _response(payload={'reports': single_page, 'total_items': 150})

        reports = MailchimpClient('token_123', 'us18').list_campaign_reports(limit=150)

        self.assertEqual(len(reports), 150)
        self.assertEqual(reports[0]['id'], 'cmp_0')
        self.assertEqual(mock_request.call_count, 1)
        call = mock_request.call_args_list[0]
        self.assertEqual(call.kwargs['headers']['Authorization'], 'Bearer token_123')
        self.assertEqual(call.kwargs['params']['count'], 150)
        self.assertEqual(call.kwargs['params']['offset'], 0)
        self.assertIn('reports.id', call.kwargs['params']['fields'])
        self.assertIn('total_items', call.kwargs['params']['fields'])

    @patch('tickets.services.mailchimp.requests.request')
    def test_list_campaign_reports_unbounded_paginates_until_total_items(self, mock_request):
        pages = [
            [_report(f'cmp_{i}') for i in range(1000)],
            [_report(f'cmp_{i}') for i in range(1000, 2000)],
            [_report(f'cmp_{i}') for i in range(2000, 3000)],
            [_report(f'cmp_{i}') for i in range(3000, 3250)],
        ]
        total = 3250
        mock_request.side_effect = [
            _response(payload={'reports': page, 'total_items': total}) for page in pages
        ]

        reports = MailchimpClient('token_123', 'us18').list_campaign_reports()

        self.assertEqual(len(reports), total)
        self.assertEqual(mock_request.call_count, 4)
        offsets = [c.kwargs['params']['offset'] for c in mock_request.call_args_list]
        self.assertEqual(offsets, [0, 1000, 2000, 3000])
        for call in mock_request.call_args_list:
            self.assertEqual(call.kwargs['params']['count'], 1000)

    @patch('tickets.services.mailchimp.requests.request')
    def test_list_campaign_reports_unbounded_stops_on_empty_page(self, mock_request):
        full_page = [_report(f'cmp_{i}') for i in range(1000)]
        mock_request.side_effect = [
            _response(payload={'reports': full_page}),
            _response(payload={'reports': []}),
        ]

        reports = MailchimpClient('token_123', 'us18').list_campaign_reports()

        self.assertEqual(len(reports), 1000)
        self.assertEqual(mock_request.call_count, 2)

    @patch('tickets.services.mailchimp.requests.request')
    def test_list_campaign_reports_bounded_returns_exactly_limit(self, mock_request):
        page = [_report(f'cmp_{i}') for i in range(200)]
        mock_request.return_value = _response(payload={'reports': page, 'total_items': 5000})

        reports = MailchimpClient('token_123', 'us18').list_campaign_reports(limit=200)

        self.assertEqual(len(reports), 200)
        self.assertEqual(mock_request.call_count, 1)
        self.assertEqual(mock_request.call_args_list[0].kwargs['params']['count'], 200)

    @patch('tickets.services.mailchimp.requests.request')
    def test_list_campaign_reports_mid_fetch_failure_raises(self, mock_request):
        first_page = [_report(f'cmp_{i}') for i in range(1000)]
        mock_request.side_effect = [
            _response(payload={'reports': first_page, 'total_items': 3000}),
            _response(status_code=500, payload={'detail': 'Mailchimp is down'}),
        ]

        with self.assertRaisesMessage(MailchimpAPIError, 'Mailchimp is down'):
            MailchimpClient('token_123', 'us18').list_campaign_reports()

    @patch('tickets.services.mailchimp.requests.request')
    def test_get_campaign_report_uses_report_endpoint(self, mock_request):
        mock_request.return_value = _response(payload=_report('cmp_1', 'Campaign 1'))

        report = MailchimpClient('token_123', 'us18').get_campaign_report('cmp_1')

        self.assertEqual(report['id'], 'cmp_1')
        self.assertEqual(mock_request.call_args.args[:2], ('GET', 'https://us18.api.mailchimp.com/3.0/reports/cmp_1'))
        self.assertIn('id', mock_request.call_args.kwargs['params']['fields'])

    @patch('tickets.services.mailchimp.requests.request')
    def test_mailchimp_error_raises_message(self, mock_request):
        mock_request.return_value = _response(status_code=401, payload={'detail': 'API key is invalid'})

        with self.assertRaisesMessage(MailchimpAPIError, 'API key is invalid'):
            MailchimpClient('bad', 'us18').get_account_root()

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

    @override_settings(MAILCHIMP_MATCH_MAX_CANDIDATES=10)
    @patch('langchain_openai.ChatOpenAI')
    def test_rank_returns_top_candidates_sorted(self, mock_chat):
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

    def test_prefilter_returns_input_when_under_max(self):
        org = Organization.objects.create(name='Org', slug='org')
        event = MagicMock(start_date=date(2026, 6, 1))
        reports = [_report(f'cmp_{i}', send_time='2026-05-15T12:00:00+00:00') for i in range(10)]

        result = MailchimpCampaignMatcher(org)._prefilter_reports(event, reports)

        self.assertEqual(len(result), 10)
        self.assertEqual([r['id'] for r in result], [f'cmp_{i}' for i in range(10)])

    @override_settings(MAILCHIMP_MATCH_PREFILTER_MAX=5)
    def test_prefilter_returns_proximity_sorted_slice_when_over_max(self):
        org = Organization.objects.create(name='Org', slug='org')
        anchor = date(2026, 6, 1)
        event = MagicMock(start_date=anchor)
        days_offsets = [-365, -180, -7, 0, 1, 14, 90, 200]
        reports = [
            _report(
                f'cmp_{offset}',
                send_time=(anchor + timedelta(days=offset)).isoformat() + 'T12:00:00+00:00',
            )
            for offset in days_offsets
        ]

        result = MailchimpCampaignMatcher(org)._prefilter_reports(event, reports)

        self.assertEqual(len(result), 5)
        self.assertEqual([r['id'] for r in result], ['cmp_0', 'cmp_1', 'cmp_-7', 'cmp_14', 'cmp_90'])

    def test_prefilter_drops_reports_without_send_time(self):
        org = Organization.objects.create(name='Org', slug='org')
        event = MagicMock(start_date=date(2026, 6, 1))
        reports = [
            _report('sent', send_time='2026-05-15T12:00:00+00:00'),
            _report('draft', send_time=None),
            _report('also_draft', send_time=''),
            _report('bad_format', send_time='not-a-date'),
        ]

        result = MailchimpCampaignMatcher(org)._prefilter_reports(event, reports)

        self.assertEqual([r['id'] for r in result], ['sent'])

    def test_build_prompt_excludes_heavy_engagement_fields(self):
        # Regression guard: heavy nested Mailchimp objects pushed the prompt past the
        # OpenAI 30k TPM ceiling. They must not reappear in the prompt payload.
        org = Organization.objects.create(name='Org', slug='org')
        venue = Venue.objects.create(organization=org, name='Room', city='Oakland')
        event = Event.objects.create(
            organization=org,
            venue=venue,
            name='Night Market',
            start_date=date(2026, 6, 1),
        )

        prompt = MailchimpCampaignMatcher(org)._build_prompt(event, [_report()])

        for forbidden in (
            'opens', 'clicks', 'ecommerce', 'archive_url',
            'emails_sent', 'open_rate', 'total_revenue', 'bounces',
        ):
            self.assertNotIn(
                forbidden, prompt,
                f"{forbidden!r} must not appear in prompt (regression of 30k TPM bug)",
            )
        self.assertIn('campaign_title', prompt)
        self.assertIn('subject_line', prompt)
        self.assertIn('send_time', prompt)
        self.assertIn('list_name', prompt)

    def test_system_prompt_includes_confidence_calibration(self):
        org = Organization.objects.create(name='Org', slug='org')

        system_prompt = MailchimpCampaignMatcher(org)._build_system_prompt(50)

        self.assertIn('HIGH (0.7', system_prompt)
        self.assertIn('MEDIUM (0.4', system_prompt)
        self.assertIn('LOW', system_prompt)
        self.assertIn('list_name', system_prompt)

    def test_system_prompt_injects_org_campaign_title_hints(self):
        org = Organization.objects.create(
            name='Org', slug='org',
            mailchimp_campaign_title_hints='lv = Las Vegas, dates in MMDDYYYY.',
        )

        system_prompt = MailchimpCampaignMatcher(org)._build_system_prompt(50)

        self.assertIn('lv = Las Vegas, dates in MMDDYYYY.', system_prompt)
        self.assertIn('hints about their campaign', system_prompt)

    def test_system_prompt_omits_hints_block_when_unset(self):
        org = Organization.objects.create(name='Org', slug='org')

        system_prompt = MailchimpCampaignMatcher(org)._build_system_prompt(50)

        self.assertNotIn('hints about their campaign', system_prompt)


@override_settings(MAILCHIMP_REPORTS_CACHE_TTL=900)
class FetchOrgReportsCachedTests(TestCase):
    def setUp(self):
        django_cache.clear()
        self.org = Organization.objects.create(
            name='Cache Org',
            slug='cache-org',
            mailchimp_access_token='token_A',
            mailchimp_dc='us18',
            mailchimp_account_id='acct_A',
        )

    @patch('tickets.services.mailchimp.MailchimpClient')
    def test_first_call_hits_api_second_call_hits_cache(self, mock_client_cls):
        reports = [_report('cmp_1'), _report('cmp_2')]
        mock_client_cls.return_value.list_campaign_reports.return_value = reports

        first = fetch_org_reports_cached(self.org)
        second = fetch_org_reports_cached(self.org)

        self.assertEqual(first, reports)
        self.assertEqual(second, reports)
        self.assertEqual(mock_client_cls.return_value.list_campaign_reports.call_count, 1)
        mock_client_cls.assert_called_once_with('token_A', 'us18')

    @patch('tickets.services.mailchimp.MailchimpClient')
    def test_cache_key_isolates_orgs_and_accounts(self, mock_client_cls):
        other_org = Organization.objects.create(
            name='Other Org',
            slug='other-org',
            mailchimp_access_token='token_B',
            mailchimp_dc='us20',
            mailchimp_account_id='acct_B',
        )
        mock_client_cls.return_value.list_campaign_reports.side_effect = [
            [_report('A1')],
            [_report('B1')],
            [_report('A2_NEW_ACCOUNT')],
        ]

        a_reports = fetch_org_reports_cached(self.org)
        b_reports = fetch_org_reports_cached(other_org)
        # Same org, new account_id → new cache key, new fetch
        self.org.mailchimp_account_id = 'acct_A_NEW'
        a_new = fetch_org_reports_cached(self.org)

        self.assertEqual([r['id'] for r in a_reports], ['A1'])
        self.assertEqual([r['id'] for r in b_reports], ['B1'])
        self.assertEqual([r['id'] for r in a_new], ['A2_NEW_ACCOUNT'])
        self.assertEqual(mock_client_cls.return_value.list_campaign_reports.call_count, 3)

    @patch('tickets.services.mailchimp.MailchimpClient')
    def test_api_failure_does_not_populate_cache(self, mock_client_cls):
        mock_client_cls.return_value.list_campaign_reports.side_effect = [
            MailchimpAPIError('boom'),
            [_report('recovered')],
        ]

        with self.assertRaises(MailchimpAPIError):
            fetch_org_reports_cached(self.org)
        # Next call should retry the API (cache was not populated)
        recovered = fetch_org_reports_cached(self.org)

        self.assertEqual([r['id'] for r in recovered], ['recovered'])
        self.assertEqual(mock_client_cls.return_value.list_campaign_reports.call_count, 2)

    def test_unconnected_org_raises_without_api_call(self):
        self.org.mailchimp_access_token = ''
        self.org.save(update_fields=['mailchimp_access_token'])

        with self.assertRaises(MailchimpAPIError):
            fetch_org_reports_cached(self.org)


@override_settings(MAILCHIMP_CLIENT_ID='client', MAILCHIMP_CLIENT_SECRET='secret')
class MailchimpViewTests(TestCase):
    def setUp(self):
        django_cache.clear()
        self.org = Organization.objects.create(
            name='Mailchimp Org',
            slug='mailchimp-org',
            mailchimp_access_token='token_123',
            mailchimp_dc='us18',
            mailchimp_account_id='acct_123',
            mailchimp_account_name='Main Account',
            mailchimp_login_email='owner@example.com',
        )
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
        self.client.force_login(self.user)
        session = self.client.session
        session['_org_id'] = str(self.org.id)
        session.save()

    def _disconnect_org(self):
        self.org.mailchimp_access_token = ''
        self.org.mailchimp_dc = ''
        self.org.mailchimp_account_id = ''
        self.org.mailchimp_account_name = ''
        self.org.mailchimp_login_email = ''
        self.org.save(update_fields=[
            'mailchimp_access_token',
            'mailchimp_dc',
            'mailchimp_account_id',
            'mailchimp_account_name',
            'mailchimp_login_email',
        ])

    def _set_oauth_state(self, state='state_123'):
        session = self.client.session
        session['mailchimp_oauth_state'] = state
        session.save()
        return state

    def test_connect_redirects_to_mailchimp_authorize_url(self):
        self._disconnect_org()

        response = self.client.post(reverse('tickets:mailchimp_connect'))

        self.assertEqual(response.status_code, 302)
        parsed = urlparse(response.url)
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, 'login.mailchimp.com')
        self.assertEqual(params['client_id'], ['client'])
        self.assertEqual(params['response_type'], ['code'])
        self.assertTrue(params['state'][0])
        self.assertEqual(self.client.session['mailchimp_oauth_state'], params['state'][0])

    @patch('tickets.views.exchange_code_for_token')
    def test_callback_rejects_invalid_state(self, mock_exchange):
        self._set_oauth_state('expected')

        response = self.client.get(reverse('tickets:mailchimp_callback'), {
            'code': 'code_123',
            'state': 'wrong',
        })

        self.assertEqual(response.status_code, 302)
        mock_exchange.assert_not_called()

    @patch('tickets.views.MailchimpClient')
    @patch('tickets.views.get_oauth_metadata')
    @patch('tickets.views.exchange_code_for_token')
    def test_callback_stores_direct_mailchimp_credentials(self, mock_exchange, mock_metadata, mock_client_cls):
        self._disconnect_org()
        self._set_oauth_state('state_123')
        mock_exchange.return_value = 'token_new'
        mock_metadata.return_value = {
            'dc': 'us20',
            'user_id': 'user_123',
            'login': {'email': 'mailchimp@example.com'},
        }
        mock_client_cls.return_value.get_account_root.return_value = {
            'account_id': 'acct_new',
            'account_name': 'New Account',
        }

        response = self.client.get(reverse('tickets:mailchimp_callback'), {
            'code': 'code_123',
            'state': 'state_123',
        })

        self.assertEqual(response.status_code, 302)
        self.org.refresh_from_db()
        self.assertEqual(self.org.mailchimp_access_token, 'token_new')
        self.assertEqual(self.org.mailchimp_dc, 'us20')
        self.assertEqual(self.org.mailchimp_account_id, 'acct_new')
        self.assertEqual(self.org.mailchimp_account_name, 'New Account')
        self.assertEqual(self.org.mailchimp_login_email, 'mailchimp@example.com')
        mock_exchange.assert_called_once()
        mock_client_cls.assert_called_once_with('token_new', 'us20')

    @patch('tickets.views.exchange_code_for_token')
    def test_callback_api_failure_does_not_store_partial_credentials(self, mock_exchange):
        self._disconnect_org()
        self._set_oauth_state('state_123')
        mock_exchange.side_effect = MailchimpAPIError('Invalid code')

        response = self.client.get(reverse('tickets:mailchimp_callback'), {
            'code': 'code_123',
            'state': 'state_123',
        })

        self.assertEqual(response.status_code, 302)
        self.org.refresh_from_db()
        self.assertEqual(self.org.mailchimp_access_token, '')
        self.assertEqual(self.org.mailchimp_dc, '')

    def test_disconnect_clears_org_mailchimp_fields(self):
        response = self.client.post(reverse('tickets:mailchimp_disconnect'))

        self.assertEqual(response.status_code, 302)
        self.org.refresh_from_db()
        self.assertEqual(self.org.mailchimp_access_token, '')
        self.assertEqual(self.org.mailchimp_dc, '')
        self.assertEqual(self.org.mailchimp_account_id, '')
        self.assertEqual(self.org.mailchimp_account_name, '')
        self.assertEqual(self.org.mailchimp_login_email, '')

    @patch('tickets.views.MailchimpCampaignMatcher')
    @patch('tickets.views.fetch_org_reports_cached')
    def test_match_endpoint_returns_modal_json(self, mock_fetch, mock_matcher_cls):
        mock_fetch.return_value = [
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
        mock_fetch.assert_called_once_with(self.org)

    @patch('tickets.views.fetch_org_reports_cached')
    def test_match_endpoint_rejects_unconnected_org(self, mock_fetch):
        self._disconnect_org()

        response = self.client.get(
            reverse('tickets:event_mailchimp_match', kwargs={'event_id': self.event.id}),
            {'format': 'json'},
        )

        self.assertEqual(response.status_code, 400)
        mock_fetch.assert_not_called()

    @patch('tickets.views.MailchimpClient')
    @patch('tickets.views.fetch_org_reports_cached')
    def test_apply_upserts_single_mailchimp_campaign(self, mock_fetch, mock_client_cls):
        mock_fetch.return_value = [_report('cmp_1', 'Event Campaign')]
        client = mock_client_cls.return_value
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

    @patch('tickets.views.MailchimpClient')
    @patch('tickets.services.mailchimp.MailchimpClient')
    def test_regression_apply_succeeds_for_campaign_beyond_old_cap(self, mock_helper_client_cls, mock_view_client_cls):
        """An 'older' campaign — outside the legacy 200 most-recent slice — must now
        round-trip through match + apply without silently failing validation."""
        all_reports = [_report(f'cmp_{i}', f'Campaign {i}') for i in range(500)]
        older_campaign = all_reports[450]
        mock_helper_client_cls.return_value.list_campaign_reports.return_value = all_reports
        mock_view_client_cls.return_value.get_campaign_report.return_value = older_campaign
        url = reverse('tickets:event_mailchimp_apply', kwargs={'event_id': self.event.id})

        response = self.client.post(url, {
            'campaign_id': older_campaign['id'],
            'confidence': '0.81',
            'reasoning': 'Older promo.',
        })

        self.assertEqual(response.status_code, 302)
        campaigns = EventEmailCampaign.objects.filter(event=self.event, source='mailchimp', deleted_at__isnull=True)
        self.assertEqual(campaigns.count(), 1)
        self.assertEqual(campaigns.get().external_id, older_campaign['id'])
        mock_view_client_cls.return_value.get_campaign_report.assert_called_once_with(older_campaign['id'])

    @patch('tickets.views.MailchimpClient')
    @patch('tickets.views.fetch_org_reports_cached')
    def test_apply_allows_multiple_linked_campaigns(self, mock_fetch, mock_client_cls):
        mock_fetch.return_value = [
            _report('cmp_1', 'Event Campaign'),
            _report('cmp_2', 'Retargeting Campaign'),
        ]
        client = mock_client_cls.return_value
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

    @patch('tickets.views.MailchimpClient')
    @patch('tickets.views.fetch_org_reports_cached')
    def test_apply_accepts_multiple_campaign_ids_in_one_request(self, mock_fetch, mock_client_cls):
        mock_fetch.return_value = [
            _report('cmp_1', 'Event Campaign'),
            _report('cmp_2', 'Retargeting Campaign'),
        ]
        client = mock_client_cls.return_value
        client.get_campaign_report.side_effect = [
            _report('cmp_1', 'Event Campaign'),
            _report('cmp_2', 'Retargeting Campaign'),
        ]
        url = reverse('tickets:event_mailchimp_apply', kwargs={'event_id': self.event.id})

        response = self.client.post(url, {
            'campaign_id': ['cmp_1', 'cmp_2'],
            'confidence': ['0.84', '0.72'],
            'reasoning': ['Name match.', 'Date match.'],
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn('?tab=marketing', response.url)
        campaigns = EventEmailCampaign.objects.filter(event=self.event, source='mailchimp', deleted_at__isnull=True)
        self.assertEqual(campaigns.count(), 2)
        self.assertEqual(set(campaigns.values_list('external_id', flat=True)), {'cmp_1', 'cmp_2'})
        self.assertEqual(client.get_campaign_report.call_count, 2)
        flash_messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any('Linked 2 Mailchimp campaigns' in m for m in flash_messages), flash_messages)

    @patch('tickets.views.MailchimpClient')
    @patch('tickets.views.fetch_org_reports_cached')
    def test_apply_partial_failure_keeps_successes(self, mock_fetch, mock_client_cls):
        mock_fetch.return_value = [
            _report('cmp_1', 'Event Campaign'),
            _report('cmp_2', 'Retargeting Campaign'),
        ]
        client = mock_client_cls.return_value
        client.get_campaign_report.side_effect = [
            _report('cmp_1', 'Event Campaign'),
            MailchimpAPIError('boom'),
        ]
        url = reverse('tickets:event_mailchimp_apply', kwargs={'event_id': self.event.id})

        response = self.client.post(url, {
            'campaign_id': ['cmp_1', 'cmp_2'],
            'confidence': ['0.84', '0.72'],
            'reasoning': ['', ''],
        })

        self.assertEqual(response.status_code, 302)
        self.assertIn('?tab=marketing', response.url)
        campaigns = EventEmailCampaign.objects.filter(event=self.event, source='mailchimp', deleted_at__isnull=True)
        self.assertEqual(set(campaigns.values_list('external_id', flat=True)), {'cmp_1'})
        flash_messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any('cmp_2' in m for m in flash_messages), flash_messages)

    @patch('tickets.views.MailchimpClient')
    @patch('tickets.views.fetch_org_reports_cached')
    def test_apply_rejects_unknown_campaign_id(self, mock_fetch, mock_client_cls):
        mock_fetch.return_value = [_report('cmp_1', 'Event Campaign')]
        client = mock_client_cls.return_value
        url = reverse('tickets:event_mailchimp_apply', kwargs={'event_id': self.event.id})

        response = self.client.post(url, {'campaign_id': 'cmp_unknown'})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            EventEmailCampaign.objects.filter(event=self.event, deleted_at__isnull=True).count(),
            0,
        )
        client.get_campaign_report.assert_not_called()
        flash_messages = [str(m) for m in response.wsgi_request._messages]
        self.assertTrue(any('cmp_unknown' in m for m in flash_messages), flash_messages)

    @patch('tickets.views.MailchimpCampaignMatcher')
    @patch('tickets.views.fetch_org_reports_cached')
    def test_match_json_marks_linked_candidates(self, mock_fetch, mock_matcher_cls):
        EventEmailCampaign.objects.create(
            event=self.event,
            source='mailchimp',
            external_id='cmp_1',
            campaign_title='Already linked',
        )
        mock_fetch.return_value = [
            _report('cmp_1', 'Event Campaign'),
            _report('cmp_2', 'Retargeting Campaign'),
        ]
        mock_matcher_cls.return_value.rank.return_value = MailchimpMatchResult(candidates=[
            MailchimpCampaignCandidate(campaign_id='cmp_1', confidence=0.9, reasoning='Already linked.'),
            MailchimpCampaignCandidate(campaign_id='cmp_2', confidence=0.6, reasoning='Likely match.'),
        ])

        response = self.client.get(
            reverse('tickets:event_mailchimp_match', kwargs={'event_id': self.event.id}),
            {'format': 'json'},
        )

        payload = response.json()
        by_id = {item['campaign_id']: item for item in payload['candidates']}
        self.assertTrue(by_id['cmp_1']['is_linked'])
        self.assertFalse(by_id['cmp_2']['is_linked'])

    @patch('tickets.views.MailchimpClient')
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

    @patch('tickets.views.MailchimpClient')
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

    @patch('tickets.views.MailchimpClient')
    def test_refresh_failure_keeps_existing_metrics(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.get_campaign_report.side_effect = MailchimpAPIError('Mailchimp unavailable')
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
