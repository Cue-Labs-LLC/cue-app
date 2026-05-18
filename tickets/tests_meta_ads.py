from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Event, EventExpense, Organization, OrganizationMembership, UserProfile, Venue
from .services.meta_ads import MetaAdsAPIError, MetaAdsClient
from .services.meta_campaign_matcher import CampaignCandidate, MatchResult, MetaCampaignMatcher


class MetaAdsClientTests(TestCase):
    @patch('tickets.services.meta_ads.requests.get')
    def test_list_campaigns_normalizes_account_and_paginates(self, mock_get):
        first_response = MagicMock(status_code=200)
        first_response.json.return_value = {
            'data': [{'id': '1', 'name': 'Campaign 1'}],
            'paging': {'next': 'https://graph.facebook.com/page-2'},
        }
        second_response = MagicMock(status_code=200)
        second_response.json.return_value = {'data': [{'id': '2', 'name': 'Campaign 2'}]}
        mock_get.side_effect = [first_response, second_response]

        campaigns = MetaAdsClient('token').list_campaigns('act_123')

        self.assertEqual([item['id'] for item in campaigns], ['1', '2'])
        self.assertIn('/act_123/campaigns', mock_get.call_args_list[0].args[0])

    @patch('tickets.services.meta_ads.requests.get')
    def test_get_campaign_spend_returns_decimal(self, mock_get):
        response = MagicMock(status_code=200)
        response.json.return_value = {'data': [{'spend': '123.456'}]}
        mock_get.return_value = response

        spend = MetaAdsClient('token').get_campaign_spend('campaign-1')

        self.assertEqual(spend, Decimal('123.46'))

    @patch('tickets.services.meta_ads.requests.get')
    def test_api_error_raises_message(self, mock_get):
        response = MagicMock(status_code=400)
        response.json.return_value = {'error': {'message': 'Bad token'}}
        mock_get.return_value = response

        with self.assertRaisesMessage(MetaAdsAPIError, 'Bad token'):
            MetaAdsClient('token').list_ad_accounts()


class MetaCampaignMatcherTests(TestCase):
    def test_empty_campaigns_short_circuits(self):
        org = Organization.objects.create(name='Org', slug='org')
        matcher = MetaCampaignMatcher(org)

        result = matcher.rank(MagicMock(), [])

        self.assertEqual(result.candidates, [])

    @patch('langchain_openai.ChatOpenAI')
    def test_rank_returns_top_three_sorted(self, mock_chat):
        org = Organization.objects.create(name='Org', slug='org')
        venue = Venue.objects.create(organization=org, name='Room', city='Oakland')
        event = Event.objects.create(
            organization=org,
            venue=venue,
            name='Night Market',
            start_date=date(2025, 5, 1),
        )
        structured = MagicMock()
        structured.invoke.return_value = MatchResult(candidates=[
            CampaignCandidate(campaign_id='low', confidence=0.2, reasoning='Weak overlap.'),
            CampaignCandidate(campaign_id='high', confidence=0.9, reasoning='Name and date match.'),
            CampaignCandidate(campaign_id='mid', confidence=0.5, reasoning='Date match.'),
            CampaignCandidate(campaign_id='extra', confidence=0.4, reasoning='Some overlap.'),
        ])
        mock_chat.return_value.with_structured_output.return_value = structured

        result = MetaCampaignMatcher(org).rank(event, [{'id': 'high', 'name': 'Night Market'}])

        self.assertEqual([item.campaign_id for item in result.candidates], ['high', 'mid', 'extra'])


class MetaAdsApplyViewTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name='Meta Org',
            slug='meta-org',
            meta_ads_access_token='token',
            meta_ads_account_id='act_123',
            meta_ads_account_name='Main Ads',
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

    @patch('tickets.views.MetaAdsClient')
    def test_apply_upserts_single_meta_campaign_expense(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.list_campaigns.return_value = [{'id': 'cmp_1', 'name': 'Event Campaign'}]
        client.get_campaign_spend.side_effect = [Decimal('42.00'), Decimal('55.50')]
        url = reverse('tickets:event_meta_ads_apply', kwargs={'event_id': self.event.id})

        first = self.client.post(url, {'campaign_id': 'cmp_1'})
        second = self.client.post(url, {'campaign_id': 'cmp_1'})

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        expenses = EventExpense.objects.filter(event=self.event, source='meta_ads', deleted_at__isnull=True)
        self.assertEqual(expenses.count(), 1)
        expense = expenses.get()
        self.assertEqual(expense.amount, Decimal('55.50'))
        self.assertEqual(expense.category, 'marketing')
        self.assertEqual(expense.external_id, 'cmp_1')
        self.assertEqual(expense.external_metadata['campaign_name'], 'Event Campaign')
        self.assertEqual(expense.version, 2)

    @patch('tickets.views.MetaAdsClient')
    def test_apply_allows_multiple_linked_campaigns(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.list_campaigns.return_value = [
            {'id': 'cmp_1', 'name': 'Event Campaign'},
            {'id': 'cmp_2', 'name': 'Retargeting Campaign'},
        ]
        client.get_campaign_spend.side_effect = [Decimal('42.00'), Decimal('18.25')]
        url = reverse('tickets:event_meta_ads_apply', kwargs={'event_id': self.event.id})

        first = self.client.post(url, {'campaign_id': 'cmp_1'})
        second = self.client.post(url, {'campaign_id': 'cmp_2'})

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        expenses = EventExpense.objects.filter(event=self.event, source='meta_ads', deleted_at__isnull=True)
        self.assertEqual(expenses.count(), 2)
        self.assertEqual(
            set(expenses.values_list('external_id', flat=True)),
            {'cmp_1', 'cmp_2'},
        )

    @patch('tickets.views.MetaCampaignMatcher')
    @patch('tickets.views.MetaAdsClient')
    def test_match_endpoint_returns_modal_json(self, mock_client_cls, mock_matcher_cls):
        client = mock_client_cls.return_value
        client.list_campaigns.return_value = [{
            'id': 'cmp_1',
            'name': 'Event Campaign',
            'objective': 'OUTCOME_TRAFFIC',
            'start_time': '2026-05-08T15:00:00+0000',
            'stop_time': '2026-05-09T06:30:00+0000',
        }]
        mock_matcher_cls.return_value.rank.return_value = MatchResult(candidates=[
            CampaignCandidate(campaign_id='cmp_1', confidence=0.84, reasoning='Name and date match.'),
        ])

        response = self.client.get(
            reverse('tickets:event_meta_ads_match', kwargs={'event_id': self.event.id}),
            {'format': 'json'},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['candidates'][0]['campaign_id'], 'cmp_1')
        self.assertEqual(payload['candidates'][0]['confidence_pct'], 84)
        self.assertEqual(payload['candidates'][0]['start_time'], 'Friday, May 8th 2026 at 8:00 AM PST')
        self.assertEqual(payload['candidates'][0]['stop_time'], 'Friday, May 8th 2026 at 11:30 PM PST')

    def test_event_detail_uses_meta_ads_modal_button(self):
        response = self.client.get(reverse('tickets:event_detail', kwargs={'event_id': self.event.id}))

        self.assertContains(response, 'data-bs-target="#metaAdsMatchModal"')
        self.assertContains(response, 'data-match-url="')
        self.assertNotContains(response, '<a href="/events/{}/meta-ads/match/" class="btn btn-sm btn-primary">'.format(self.event.id))

    @patch('tickets.views.django_tz.now')
    @patch('tickets.views.MetaAdsClient')
    def test_event_detail_lists_linked_meta_campaigns(self, mock_client_cls, mock_now):
        mock_now.return_value = datetime(2026, 5, 8, 15, 0, tzinfo=timezone.utc)
        client = mock_client_cls.return_value
        client.get_campaign_spend.side_effect = [Decimal('44.00'), Decimal('12.75')]
        EventExpense.objects.create(
            event=self.event,
            category='marketing',
            source='meta_ads',
            description='Meta Ads: Event Campaign',
            amount=Decimal('44.00'),
            external_id='cmp_1',
            external_metadata={
                'campaign_name': 'Event Campaign',
                'last_synced_at': '2026-05-08T15:00:00+00:00',
            },
        )
        EventExpense.objects.create(
            event=self.event,
            category='marketing',
            source='meta_ads',
            description='Meta Ads: Retargeting Campaign',
            amount=Decimal('12.75'),
            external_id='cmp_2',
            external_metadata={
                'campaign_name': 'Retargeting Campaign',
                'last_synced_at': '2026-05-08T16:00:00+00:00',
            },
        )

        response = self.client.get(reverse('tickets:event_detail', kwargs={'event_id': self.event.id}))

        self.assertContains(response, 'Linked Campaign')
        self.assertNotContains(response, 'Last Synced')
        self.assertNotContains(response, 'Re-match')
        self.assertContains(response, 'Refresh')
        self.assertContains(response, 'Remove')
        self.assertContains(response, 'Event Campaign')
        self.assertContains(response, 'Retargeting Campaign')
        self.assertContains(response, 'cmp_1')
        self.assertContains(response, 'cmp_2')
        self.assertContains(response, '$44.00')
        self.assertContains(response, '$12.75')

    @patch('tickets.views.MetaAdsClient')
    def test_refresh_action_updates_only_selected_linked_campaign(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.get_campaign_spend.return_value = Decimal('99.00')
        selected = EventExpense.objects.create(
            event=self.event,
            category='marketing',
            source='meta_ads',
            description='Meta Ads: Event Campaign',
            amount=Decimal('44.00'),
            external_id='cmp_1',
            external_metadata={'campaign_name': 'Event Campaign'},
        )
        other = EventExpense.objects.create(
            event=self.event,
            category='marketing',
            source='meta_ads',
            description='Meta Ads: Retargeting Campaign',
            amount=Decimal('12.75'),
            external_id='cmp_2',
            external_metadata={'campaign_name': 'Retargeting Campaign'},
        )

        response = self.client.post(reverse('tickets:event_meta_ads_refresh', kwargs={
            'event_id': self.event.id,
            'expense_id': selected.id,
        }))

        self.assertEqual(response.status_code, 302)
        self.assertIn('?tab=marketing', response.url)
        selected.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(selected.amount, Decimal('99.00'))
        self.assertEqual(selected.version, 2)
        self.assertIn('last_synced_at', selected.external_metadata)
        self.assertEqual(other.amount, Decimal('12.75'))
        self.assertEqual(other.version, 1)
        client.get_campaign_spend.assert_called_once_with('cmp_1')

    @patch('tickets.views.MetaAdsClient')
    def test_remove_action_soft_deletes_only_selected_linked_campaign(self, mock_client_cls):
        selected = EventExpense.objects.create(
            event=self.event,
            category='marketing',
            source='meta_ads',
            description='Meta Ads: Event Campaign',
            amount=Decimal('44.00'),
            external_id='cmp_1',
            external_metadata={'campaign_name': 'Event Campaign'},
        )
        other = EventExpense.objects.create(
            event=self.event,
            category='marketing',
            source='meta_ads',
            description='Meta Ads: Retargeting Campaign',
            amount=Decimal('12.75'),
            external_id='cmp_2',
            external_metadata={'campaign_name': 'Retargeting Campaign'},
        )

        response = self.client.post(reverse('tickets:event_meta_ads_remove', kwargs={
            'event_id': self.event.id,
            'expense_id': selected.id,
        }))

        self.assertEqual(response.status_code, 302)
        self.assertIn('?tab=marketing', response.url)
        selected.refresh_from_db()
        other.refresh_from_db()
        self.assertIsNotNone(selected.deleted_at)
        self.assertIsNone(other.deleted_at)
        mock_client_cls.assert_not_called()

    def test_meta_ads_row_actions_404_for_non_meta_or_wrong_event_expense(self):
        other_org = Organization.objects.create(name='Other Org', slug='other-org')
        other_venue = Venue.objects.create(organization=other_org, name='Other Venue', city='Oakland')
        other_event = Event.objects.create(
            organization=other_org,
            venue=other_venue,
            name='Other Event',
            start_date=date(2025, 6, 2),
        )
        other_expense = EventExpense.objects.create(
            event=other_event,
            category='marketing',
            source='meta_ads',
            description='Meta Ads: Other Campaign',
            amount=Decimal('10.00'),
            external_id='cmp_other',
        )
        manual_expense = EventExpense.objects.create(
            event=self.event,
            category='marketing',
            source='manual',
            description='Manual marketing',
            amount=Decimal('10.00'),
        )

        refresh_other = self.client.post(reverse('tickets:event_meta_ads_refresh', kwargs={
            'event_id': self.event.id,
            'expense_id': other_expense.id,
        }))
        remove_other = self.client.post(reverse('tickets:event_meta_ads_remove', kwargs={
            'event_id': self.event.id,
            'expense_id': other_expense.id,
        }))
        refresh_manual = self.client.post(reverse('tickets:event_meta_ads_refresh', kwargs={
            'event_id': self.event.id,
            'expense_id': manual_expense.id,
        }))
        remove_manual = self.client.post(reverse('tickets:event_meta_ads_remove', kwargs={
            'event_id': self.event.id,
            'expense_id': manual_expense.id,
        }))

        self.assertEqual(refresh_other.status_code, 404)
        self.assertEqual(remove_other.status_code, 404)
        self.assertEqual(refresh_manual.status_code, 404)
        self.assertEqual(remove_manual.status_code, 404)

    @patch('tickets.views.MetaAdsClient')
    def test_refresh_action_failure_keeps_existing_amount(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.get_campaign_spend.side_effect = MetaAdsAPIError('Meta unavailable')
        expense = EventExpense.objects.create(
            event=self.event,
            category='marketing',
            source='meta_ads',
            description='Meta Ads: Event Campaign',
            amount=Decimal('44.00'),
            external_id='cmp_1',
            external_metadata={'campaign_name': 'Event Campaign'},
        )

        response = self.client.post(reverse('tickets:event_meta_ads_refresh', kwargs={
            'event_id': self.event.id,
            'expense_id': expense.id,
        }))

        self.assertEqual(response.status_code, 302)
        self.assertIn('?tab=marketing', response.url)
        expense.refresh_from_db()
        self.assertEqual(expense.amount, Decimal('44.00'))
        self.assertEqual(expense.version, 1)

    @patch('tickets.views.MetaAdsClient')
    def test_event_detail_refreshes_linked_campaign_spend_before_render(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.get_campaign_spend.return_value = Decimal('88.25')
        expense = EventExpense.objects.create(
            event=self.event,
            category='marketing',
            source='meta_ads',
            description='Meta Ads: Event Campaign',
            amount=Decimal('44.00'),
            external_id='cmp_1',
            external_metadata={'campaign_name': 'Event Campaign'},
        )

        response = self.client.get(reverse('tickets:event_detail', kwargs={'event_id': self.event.id}))

        self.assertEqual(response.status_code, 200)
        expense.refresh_from_db()
        self.assertEqual(expense.amount, Decimal('88.25'))
        self.assertEqual(expense.version, 2)
        self.assertIn('last_synced_at', expense.external_metadata)
        self.assertContains(response, '$88.25')
        self.assertContains(response, 'Expenses')
        self.assertContains(response, '$88.25')
        client.get_campaign_spend.assert_called_once_with('cmp_1')

    @patch('tickets.views.MetaAdsClient')
    def test_event_detail_refreshes_multiple_campaigns_and_finance_total(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.get_campaign_spend.side_effect = [Decimal('50.00'), Decimal('25.50')]
        # Confirmed expenses participate in the finance total. Unconfirmed
        # campaign-matched expenses are intentionally excluded from event
        # expense totals.
        EventExpense.objects.create(
            event=self.event,
            category='marketing',
            source='meta_ads',
            description='Meta Ads: Event Campaign',
            amount=Decimal('1.00'),
            external_id='cmp_1',
            external_metadata={'campaign_name': 'Event Campaign'},
            confirmed_at=datetime.now(timezone.utc),
        )
        EventExpense.objects.create(
            event=self.event,
            category='marketing',
            source='meta_ads',
            description='Meta Ads: Retargeting Campaign',
            amount=Decimal('2.00'),
            external_id='cmp_2',
            external_metadata={'campaign_name': 'Retargeting Campaign'},
            confirmed_at=datetime.now(timezone.utc),
        )

        response = self.client.get(reverse('tickets:event_detail', kwargs={'event_id': self.event.id}))

        self.assertEqual(response.status_code, 200)
        amounts = dict(
            EventExpense.objects.filter(event=self.event, source='meta_ads')
            .values_list('external_id', 'amount')
        )
        self.assertEqual(amounts['cmp_1'], Decimal('50.00'))
        self.assertEqual(amounts['cmp_2'], Decimal('25.50'))
        self.assertContains(response, '$75.50')
        self.assertEqual(client.get_campaign_spend.call_count, 2)

    @patch('tickets.views.MetaAdsClient')
    def test_event_detail_refresh_failure_keeps_page_rendering_and_stored_amount(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.get_campaign_spend.side_effect = MetaAdsAPIError('Meta unavailable')
        expense = EventExpense.objects.create(
            event=self.event,
            category='marketing',
            source='meta_ads',
            description='Meta Ads: Event Campaign',
            amount=Decimal('44.00'),
            external_id='cmp_1',
            external_metadata={'campaign_name': 'Event Campaign'},
        )

        response = self.client.get(reverse('tickets:event_detail', kwargs={'event_id': self.event.id}))

        self.assertEqual(response.status_code, 200)
        expense.refresh_from_db()
        self.assertEqual(expense.amount, Decimal('44.00'))
        self.assertEqual(expense.version, 1)
        self.assertNotIn('last_synced_at', expense.external_metadata)
        self.assertContains(response, '$44.00')

    @patch('tickets.views.MetaAdsClient')
    def test_event_detail_without_meta_credentials_does_not_refresh(self, mock_client_cls):
        self.org.meta_ads_access_token = ''
        self.org.meta_ads_account_id = ''
        self.org.save(update_fields=['meta_ads_access_token', 'meta_ads_account_id'])
        EventExpense.objects.create(
            event=self.event,
            category='marketing',
            source='meta_ads',
            description='Meta Ads: Event Campaign',
            amount=Decimal('44.00'),
            external_id='cmp_1',
            external_metadata={'campaign_name': 'Event Campaign'},
        )

        response = self.client.get(reverse('tickets:event_detail', kwargs={'event_id': self.event.id}))

        self.assertEqual(response.status_code, 200)
        mock_client_cls.assert_not_called()
