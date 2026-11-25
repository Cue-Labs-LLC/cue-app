import json
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Event, EventExpense, MetaAdDailyInsight, Organization, OrganizationMembership, UserProfile, Venue
from .services.meta_ads import CampaignInsights, MetaAdsAPIError, MetaAdsClient
from .services.meta_campaign_matcher import CampaignCandidate, MatchResult, MetaCampaignMatcher


def _insights(spend, purchases=None, purchase_value=None):
    return CampaignInsights(
        spend=Decimal(spend),
        purchases=purchases,
        purchase_value=Decimal(purchase_value) if purchase_value is not None else None,
    )


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
    def test_get_campaign_insights_requests_actions_fields(self, mock_get):
        response = MagicMock(status_code=200)
        response.json.return_value = {'data': []}
        mock_get.return_value = response

        MetaAdsClient('token').get_campaign_insights('campaign-1')

        params = mock_get.call_args.kwargs['params']
        self.assertEqual(params['fields'], 'spend,actions,action_values')
        self.assertEqual(params['date_preset'], 'maximum')

    @patch('tickets.services.meta_ads.requests.get')
    def test_get_campaign_insights_uses_priority_purchase_type_without_summing(self, mock_get):
        response = MagicMock(status_code=200)
        response.json.return_value = {'data': [{
            'spend': '123.456',
            'actions': [
                {'action_type': 'offsite_conversion.fb_pixel_purchase', 'value': '7'},
                {'action_type': 'omni_purchase', 'value': '9.0'},
                {'action_type': 'purchase', 'value': '8'},
                {'action_type': 'link_click', 'value': '500'},
            ],
            'action_values': [
                {'action_type': 'offsite_conversion.fb_pixel_purchase', 'value': '700.10'},
                {'action_type': 'omni_purchase', 'value': '901.239'},
                {'action_type': 'purchase', 'value': '800.00'},
            ],
        }]}
        mock_get.return_value = response

        insights = MetaAdsClient('token').get_campaign_insights('campaign-1')

        self.assertEqual(insights.spend, Decimal('123.46'))
        self.assertEqual(insights.purchases, 9)
        self.assertEqual(insights.purchase_value, Decimal('901.24'))

    @patch('tickets.services.meta_ads.requests.get')
    def test_get_campaign_insights_falls_back_to_lower_priority_purchase_type(self, mock_get):
        response = MagicMock(status_code=200)
        response.json.return_value = {'data': [{
            'spend': '10',
            'actions': [{'action_type': 'purchase', 'value': '3'}],
            'action_values': [{'action_type': 'purchase', 'value': '150.00'}],
        }]}
        mock_get.return_value = response

        insights = MetaAdsClient('token').get_campaign_insights('campaign-1')

        self.assertEqual(insights.purchases, 3)
        self.assertEqual(insights.purchase_value, Decimal('150.00'))

    @patch('tickets.services.meta_ads.requests.get')
    def test_get_campaign_insights_actions_without_purchases_returns_zero(self, mock_get):
        response = MagicMock(status_code=200)
        response.json.return_value = {'data': [{
            'spend': '10.00',
            'actions': [{'action_type': 'link_click', 'value': '42'}],
            'action_values': [{'action_type': 'link_click', 'value': '0'}],
        }]}
        mock_get.return_value = response

        insights = MetaAdsClient('token').get_campaign_insights('campaign-1')

        self.assertEqual(insights.purchases, 0)
        self.assertEqual(insights.purchase_value, Decimal('0.00'))

    @patch('tickets.services.meta_ads.requests.get')
    def test_get_campaign_insights_missing_actions_returns_none(self, mock_get):
        response = MagicMock(status_code=200)
        response.json.return_value = {'data': [{'spend': '10.00'}]}
        mock_get.return_value = response

        insights = MetaAdsClient('token').get_campaign_insights('campaign-1')

        self.assertEqual(insights.spend, Decimal('10.00'))
        self.assertIsNone(insights.purchases)
        self.assertIsNone(insights.purchase_value)

    @patch('tickets.services.meta_ads.requests.get')
    def test_get_campaign_insights_empty_data_returns_zero_spend_none_attribution(self, mock_get):
        response = MagicMock(status_code=200)
        response.json.return_value = {'data': []}
        mock_get.return_value = response

        insights = MetaAdsClient('token').get_campaign_insights('campaign-1')

        self.assertEqual(insights.spend, Decimal('0.00'))
        self.assertIsNone(insights.purchases)
        self.assertIsNone(insights.purchase_value)

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

    @patch('tickets.integrations.meta_ads.MetaAdsClient')
    def test_apply_upserts_single_meta_campaign_expense(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.list_campaigns.return_value = [{'id': 'cmp_1', 'name': 'Event Campaign'}]
        client.get_campaign_insights.side_effect = [
            _insights('42.00', 5, '250.00'),
            _insights('55.50', 6, '310.75'),
        ]
        url = reverse('tickets:event_meta_ads_apply', kwargs={'event_id': self.event.id})

        first = self.client.post(url, {'campaign_id': 'cmp_1'})
        second = self.client.post(url, {'campaign_id': 'cmp_1'})

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        expenses = EventExpense.objects.filter(event=self.event, source='meta_ads', deleted_at__isnull=True)
        self.assertEqual(expenses.count(), 1)
        expense = expenses.get()
        self.assertEqual(expense.amount, Decimal('55.50'))
        self.assertEqual(expense.api_attributed_orders, 6)
        self.assertEqual(expense.api_attributed_revenue, Decimal('310.75'))
        self.assertEqual(expense.category, 'marketing')
        self.assertEqual(expense.external_id, 'cmp_1')
        self.assertEqual(expense.external_metadata['campaign_name'], 'Event Campaign')
        self.assertEqual(expense.version, 2)

    @patch('tickets.integrations.meta_ads.MetaAdsClient')
    def test_apply_allows_multiple_linked_campaigns(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.list_campaigns.return_value = [
            {'id': 'cmp_1', 'name': 'Event Campaign'},
            {'id': 'cmp_2', 'name': 'Retargeting Campaign'},
        ]
        client.get_campaign_insights.side_effect = [_insights('42.00'), _insights('18.25')]
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

    @patch('tickets.integrations.meta_ads.MetaCampaignMatcher')
    @patch('tickets.integrations.meta_ads.MetaAdsClient')
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

    @patch('tickets.integrations.meta_ads.django_tz.now')
    @patch('tickets.integrations.meta_ads.MetaAdsClient')
    def test_event_detail_lists_linked_meta_campaigns(self, mock_client_cls, mock_now):
        mock_now.return_value = datetime(2026, 5, 8, 15, 0, tzinfo=timezone.utc)
        client = mock_client_cls.return_value
        client.get_campaign_insights.side_effect = [_insights('44.00'), _insights('12.75')]
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

    @patch('tickets.integrations.meta_ads.MetaAdsClient')
    def test_refresh_action_updates_only_selected_linked_campaign(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.get_campaign_insights.return_value = _insights('99.00', 4, '200.00')
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
        self.assertEqual(selected.api_attributed_orders, 4)
        self.assertEqual(selected.api_attributed_revenue, Decimal('200.00'))
        self.assertEqual(selected.version, 2)
        self.assertIn('last_synced_at', selected.external_metadata)
        self.assertEqual(other.amount, Decimal('12.75'))
        self.assertEqual(other.version, 1)
        client.get_campaign_insights.assert_called_once_with('cmp_1')

    @patch('tickets.integrations.meta_ads.MetaAdsClient')
    def test_refresh_flags_confirmed_expense_when_attribution_changes(self, mock_client_cls):
        client = mock_client_cls.return_value
        # Spend unchanged; only API attribution moved.
        client.get_campaign_insights.return_value = _insights('44.00', 9, '450.00')
        expense = EventExpense.objects.create(
            event=self.event,
            category='marketing',
            source='meta_ads',
            description='Meta Ads: Event Campaign',
            amount=Decimal('44.00'),
            api_attributed_orders=4,
            api_attributed_revenue=Decimal('200.00'),
            external_id='cmp_1',
            external_metadata={'campaign_name': 'Event Campaign'},
            confirmed_at=datetime.now(timezone.utc),
        )

        response = self.client.post(reverse('tickets:event_meta_ads_refresh', kwargs={
            'event_id': self.event.id,
            'expense_id': expense.id,
        }))

        self.assertEqual(response.status_code, 302)
        expense.refresh_from_db()
        self.assertEqual(expense.amount, Decimal('44.00'))
        self.assertEqual(expense.api_attributed_orders, 9)
        self.assertEqual(expense.api_attributed_revenue, Decimal('450.00'))
        self.assertIsNotNone(expense.api_data_changed_at)
        self.assertTrue(expense.needs_review)

    @patch('tickets.integrations.meta_ads.MetaAdsClient')
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

    @patch('tickets.integrations.meta_ads.MetaAdsClient')
    def test_refresh_action_failure_keeps_existing_amount(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.get_campaign_insights.side_effect = MetaAdsAPIError('Meta unavailable')
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
        client.get_campaign_insights.return_value = _insights('88.25', 3, '120.00')
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
        self.assertEqual(expense.api_attributed_orders, 3)
        self.assertEqual(expense.api_attributed_revenue, Decimal('120.00'))
        self.assertEqual(expense.version, 2)
        self.assertIn('last_synced_at', expense.external_metadata)
        self.assertContains(response, '$88.25')
        self.assertContains(response, 'Expenses')
        self.assertContains(response, '$88.25')
        client.get_campaign_insights.assert_called_once_with('cmp_1')

    @patch('tickets.views.MetaAdsClient')
    def test_event_detail_refreshes_multiple_campaigns_and_finance_total(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.get_campaign_insights.side_effect = [_insights('50.00'), _insights('25.50')]
        # Linked campaign-matched expenses participate in the finance total as
        # soon as they're linked — no separate confirm step is required.
        EventExpense.objects.create(
            event=self.event,
            category='marketing',
            source='meta_ads',
            description='Meta Ads: Event Campaign',
            amount=Decimal('1.00'),
            external_id='cmp_1',
            external_metadata={'campaign_name': 'Event Campaign'},
        )
        EventExpense.objects.create(
            event=self.event,
            category='marketing',
            source='meta_ads',
            description='Meta Ads: Retargeting Campaign',
            amount=Decimal('2.00'),
            external_id='cmp_2',
            external_metadata={'campaign_name': 'Retargeting Campaign'},
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
        self.assertEqual(client.get_campaign_insights.call_count, 2)

    @patch('tickets.integrations.meta_ads.MetaAdsClient')
    def test_event_detail_refresh_failure_keeps_page_rendering_and_stored_amount(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.get_campaign_insights.side_effect = MetaAdsAPIError('Meta unavailable')
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

    @patch('tickets.integrations.meta_ads.MetaAdsClient')
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


def _ad_row(spend, impressions, clicks, purchases=None, value=None, **extra):
    row = {'spend': spend, 'impressions': impressions, 'inline_link_clicks': clicks, **extra}
    if purchases is not None:
        row['actions'] = [{'action_type': 'purchase', 'value': str(purchases)}]
        row['action_values'] = [{'action_type': 'purchase', 'value': str(value)}]
    return row


class MetaAdsDashboardTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.org = Organization.objects.create(
            name='Meta Org', slug='meta-org',
            meta_ads_access_token='token', meta_ads_account_id='act_123',
            meta_ads_account_name='Main Ads',
        )
        self.user = User.objects.create_user(username='owner', email='owner@example.com', password='pass123')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.force_login(self.user)
        session = self.client.session
        session['_org_id'] = str(self.org.id)
        session.save()
        self.url = reverse('tickets:meta_ads_dashboard')

    def _mock_client(self, mock_client_cls):
        client = mock_client_cls.return_value
        campaign_rows = [
            _ad_row('100.00', 10000, 200, purchases=10, value='500.00',
                    campaign_id='c1', campaign_name='Big Campaign', account_currency='USD'),
            _ad_row('50.00', 5000, 50, campaign_id='c2', campaign_name='Awareness'),
        ]
        ad_rows = [
            _ad_row('20.00', 2000, 40, purchases=2, value='80.00',
                    ad_id='102', ad_name='Small Ad', campaign_name='Big Campaign'),
            _ad_row('80.00', 8000, 160, purchases=8, value='420.00',
                    ad_id='101', ad_name='Hero Video', campaign_name='Big Campaign', adset_name='Lookalike'),
        ]
        client.get_account_insights.side_effect = lambda account_id, level, **kw: (
            campaign_rows if level == 'campaign' else ad_rows
        )
        client.list_campaigns.return_value = [{'id': 'c1', 'status': 'ACTIVE'}, {'id': 'c2', 'status': 'PAUSED'}]
        client.get_ad_creative_keys.return_value = {}
        client.get_ad_details.return_value = {'101': {
            'image_url': 'https://cdn.example.com/a1.jpg',
            'status': 'Active', 'is_active': True, 'preview_url': 'https://fb.me/abc',
            'format': 'Video', 'is_video': True, 'headlines': ['TEMPO!'], 'bodies': ['Join us Sept 25th'],
            'description': 'LA - SEPT 25TH', 'cta': 'See details',
            'link_url': 'https://www.cueup.co/t/xyz/',
        }}
        return client

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_dashboard_totals_campaigns_and_creatives(self, mock_client_cls):
        client = self._mock_client(mock_client_cls)

        response = self.client.get(self.url, {'range': 'last_7d', 'sort': 'spend', 'group': '0'})

        self.assertEqual(response.status_code, 200)
        data = response.context['data']
        totals = data['totals']
        self.assertEqual(totals['spend'], Decimal('150.00'))
        self.assertEqual(totals['purchases'], 10)
        self.assertEqual(totals['purchase_value'], Decimal('500.00'))
        self.assertEqual(totals['roas'], Decimal('3.33'))
        self.assertEqual(totals['ctr'], Decimal('1.67'))
        self.assertEqual(totals['cost_per_purchase'], Decimal('15.00'))

        self.assertEqual([c['name'] for c in data['campaigns']], ['Big Campaign', 'Awareness'])
        self.assertEqual(data['campaigns'][0]['status'], 'ACTIVE')
        # A campaign Meta reported no actions for has unknown (not zero) purchases.
        self.assertIsNone(data['campaigns'][1]['purchases'])
        self.assertIsNone(data['campaigns'][1]['roas'])

        self.assertEqual([a['id'] for a in data['creatives']], ['101', '102'])
        self.assertEqual(data['creatives'][0]['thumbnail_url'], 'https://cdn.example.com/a1.jpg')
        self.assertEqual(data['creatives'][1]['thumbnail_url'], '')
        self.assertEqual(client.get_account_insights.call_args.kwargs['date_preset'], 'last_7d')
        self.assertEqual(data['creatives'][0]['cpm'], Decimal('10.00'))

        # Both insights queries ask Meta to rank by spend so the row cap drops the
        # smallest rows; ads come from a capped pool that creatives are ranked from.
        calls = {c.kwargs['level']: c.kwargs for c in client.get_account_insights.call_args_list}
        self.assertEqual(calls['campaign']['sort'], 'spend_descending')
        self.assertEqual(calls['ad']['sort'], 'spend_descending')
        # The ad pool is every ad with $25+ spend (filtered by Meta), not a top-N.
        self.assertEqual(calls['ad']['limit'], 2000)
        self.assertEqual(calls['ad']['min_spend'], 25)

        # The detail side panel content is rendered into a per-creative template.
        self.assertContains(response, 'id="creativePanel"')
        self.assertContains(response, 'id="creative-detail-1"')
        self.assertContains(response, 'Join us Sept 25th')
        self.assertContains(response, 'https://fb.me/abc')
        # Only the video ad gets a player hook.
        self.assertContains(response, 'data-preview-url="/analytics/meta-ads/ads/101/preview/"', count=1)
        self.assertContains(response, 'Hero Video')

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_dashboard_caches_until_refresh(self, mock_client_cls):
        client = self._mock_client(mock_client_cls)

        self.client.get(self.url)
        self.client.get(self.url)
        self.assertEqual(client.list_campaigns.call_count, 1)

        self.client.get(self.url, {'refresh': '1'})
        self.assertEqual(client.list_campaigns.call_count, 2)

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_detail_failure_still_renders(self, mock_client_cls):
        client = self._mock_client(mock_client_cls)
        client.get_ad_details.side_effect = MetaAdsAPIError('bad field')

        response = self.client.get(self.url, {'sort': 'spend'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['data']['creatives'][0]['thumbnail_url'], '')

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_api_error_shows_message(self, mock_client_cls):
        mock_client_cls.return_value.get_account_insights.side_effect = MetaAdsAPIError('Token expired')

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['data'])
        self.assertContains(response, 'Token expired')

    def test_not_connected_shows_connect_cta(self):
        self.org.meta_ads_account_id = ''
        self.org.save(update_fields=['meta_ads_account_id'])

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('tickets:meta_ads_settings'))

    def test_unknown_range_falls_back_to_default(self):
        from .services.meta_ads_dashboard import preset_date_range
        self.assertEqual(preset_date_range('bogus').date_preset, 'last_30d')

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_calendar_range_sends_time_range_from_shared_window_bounds(self, mock_client_cls):
        from .views import _window_bounds
        client = self._mock_client(mock_client_cls)

        response = self.client.get(self.url, {'range': 'last_quarter'})

        start, end = _window_bounds('last_quarter')
        kwargs = client.get_account_insights.call_args.kwargs
        self.assertEqual(kwargs['time_range'], (start, end))
        self.assertEqual(kwargs['date_preset'], '')
        self.assertEqual(response.context['active_range_label'], 'Last Quarter')
        self.assertContains(response, 'Month-To-Date')  # profitability options in the menu

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_this_year_is_capped_at_today(self, mock_client_cls):
        from datetime import date
        client = self._mock_client(mock_client_cls)

        self.client.get(self.url, {'range': 'this_year'})

        since, until = client.get_account_insights.call_args.kwargs['time_range']
        self.assertEqual(since, date(date.today().year, 1, 1))
        self.assertEqual(until, date.today())

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_custom_range_uses_dates_and_keeps_them_in_links(self, mock_client_cls):
        from datetime import date
        client = self._mock_client(mock_client_cls)

        response = self.client.get(self.url, {
            'range': 'custom', 'start': '2026-03-01', 'end': '2026-04-15', 'sort': 'purchases',
        })

        self.assertEqual(
            client.get_account_insights.call_args.kwargs['time_range'],
            (date(2026, 3, 1), date(2026, 4, 15)),
        )
        self.assertEqual(response.context['active_range_label'], 'Mar 1, 2026 \u2013 Apr 15, 2026')
        self.assertContains(response, 'name="start" class="form-control form-control-sm" value="2026-03-01"')
        # Refresh and creative controls carry the custom dates.
        self.assertContains(response, 'href="?range=custom&amp;start=2026-03-01&amp;end=2026-04-15&amp;sort=purchases')
        self.assertContains(response, '<input type="hidden" name="start" value="2026-03-01">')

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_all_window_maps_to_lifetime(self, mock_client_cls):
        client = self._mock_client(mock_client_cls)
        response = self.client.get(self.url, {'range': 'all'})
        self.assertEqual(client.get_account_insights.call_args.kwargs['date_preset'], 'maximum')
        self.assertEqual(response.context['active_range'], 'maximum')

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_ranges_are_cached_separately(self, mock_client_cls):
        client = self._mock_client(mock_client_cls)
        self.client.get(self.url, {'range': 'last_month'})
        self.client.get(self.url, {'range': 'last_year'})
        self.client.get(self.url, {'range': 'last_month'})
        self.assertEqual(client.get_account_insights.call_count, 4)  # 2 ranges x (campaign + ad)

    def test_calendar_date_range_clamps_to_meta_limits(self):
        from datetime import date
        from .services.meta_ads_dashboard import calendar_date_range
        today = date(2026, 11, 17)

        future = calendar_date_range('this_month', 'This Month', date(2026, 11, 1), date(2026, 11, 30), today)
        self.assertEqual((future.since, future.until), (date(2026, 11, 1), today))

        ancient = calendar_date_range('custom', '', date(2015, 1, 1), date(2016, 1, 1), today)
        self.assertEqual(ancient.since, date(2023, 11, 1))  # Meta's ~37-month history floor
        self.assertEqual(ancient.until, date(2023, 11, 1))  # whole range predates it

        open_start = calendar_date_range('custom', '', None, date(2026, 5, 1), today)
        self.assertEqual(open_start.since, date(2023, 11, 1))
        self.assertEqual(open_start.label, 'Nov 1, 2023 \u2013 May 1, 2026')

    @patch('tickets.services.meta_ads.requests.get')
    def test_get_ad_details_reads_copy_from_each_creative_shape(self, mock_get):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            # Video ad: copy lives in object_story_spec.video_data.
            'a1': {
                'effective_status': 'CAMPAIGN_PAUSED',
                'preview_shareable_link': 'https://fb.me/a1',
                'creative': {
                    'thumbnail_url': 'thumb.jpg', 'object_type': 'VIDEO',
                    'object_story_spec': {'video_data': {
                        'title': 'TEMPO!', 'message': 'Join us', 'link_description': 'LA',
                        'call_to_action': {'type': 'SEE_DETAILS', 'value': {'link': 'https://cueup.co/t/1/'}},
                    }},
                },
            },
            # Dynamic creative: several variants in asset_feed_spec; the creative's
            # own title duplicates the first variant and must not repeat.
            'a2': {
                'effective_status': 'ACTIVE',
                'creative': {
                    'image_url': 'https://cdn/full.jpg', 'thumbnail_url': 'https://cdn/thumb.jpg',
                    'title': 'Party',
                    'asset_feed_spec': {
                        'titles': [{'text': 'Party'}, {'text': 'Party 2'}],
                        'bodies': [{'text': 'Body A'}, {'text': 'Body B'}],
                        'call_to_action_types': ['BUY_TICKETS'],
                        'link_urls': [{'website_url': 'javascript:alert(1)'}],
                    },
                },
            },
            'a3': {},
        }
        mock_get.return_value = response

        result = MetaAdsClient('token').get_ad_details(['a1', 'a2', 'a3'])

        self.assertEqual(mock_get.call_args.kwargs['params']['ids'], 'a1,a2,a3')
        video = result['a1']
        self.assertEqual(video['status'], 'Campaign paused')
        self.assertFalse(video['is_active'])
        self.assertEqual(video['format'], 'Video')
        self.assertEqual(video['headlines'], ['TEMPO!'])
        self.assertEqual(video['bodies'], ['Join us'])
        self.assertEqual(video['description'], 'LA')
        self.assertEqual(video['cta'], 'See details')
        self.assertEqual(video['link_url'], 'https://cueup.co/t/1/')
        self.assertEqual(video['preview_url'], 'https://fb.me/a1')
        self.assertTrue(video['is_video'])
        # Relative/non-http URLs are dropped rather than rendered as links.
        self.assertEqual(video['image_url'], '')

        dynamic = result['a2']
        self.assertTrue(dynamic['is_active'])
        self.assertEqual(dynamic['image_url'], 'https://cdn/full.jpg')
        self.assertEqual(dynamic['headlines'], ['Party', 'Party 2'])
        self.assertEqual(dynamic['bodies'], ['Body A', 'Body B'])
        self.assertEqual(dynamic['cta'], 'Buy tickets')
        self.assertEqual(dynamic['link_url'], '')
        self.assertFalse(dynamic['is_video'])

        self.assertEqual(result['a3']['headlines'], [])

    @patch('tickets.services.meta_ads.requests.get')
    def test_get_ad_preview_extracts_and_unescapes_iframe_src(self, mock_get):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            'account_id': '123',
            'previews': {'data': [{'body': (
                '<iframe src="https://business.facebook.com/ads/api/preview_iframe.php?d=AQ&amp;t=AQ" '
                'width="335" height="450"></iframe>'
            )}]},
        }
        mock_get.return_value = response

        preview = MetaAdsClient('token').get_ad_preview('987')

        self.assertEqual(preview, {
            'account_id': '123',
            'src': 'https://business.facebook.com/ads/api/preview_iframe.php?d=AQ&t=AQ',
        })
        self.assertIn('/987', mock_get.call_args.args[0])
        self.assertIn('previews.ad_format(MOBILE_FEED_STANDARD)', mock_get.call_args.kwargs['params']['fields'])

    @patch('tickets.services.meta_ads.requests.get')
    def test_get_ad_preview_rejects_non_meta_iframe(self, mock_get):
        response = MagicMock(status_code=200)
        response.json.return_value = {'account_id': '123', 'previews': {'data': [
            {'body': '<iframe src="https://evil.example.com/x"></iframe>'},
        ]}}
        mock_get.return_value = response

        with self.assertRaises(MetaAdsAPIError):
            MetaAdsClient('token').get_ad_preview('987')

    @patch('tickets.integrations.meta_ads.MetaAdsClient')
    def test_ad_preview_returns_src_and_caches(self, mock_client_cls):
        src = 'https://business.facebook.com/ads/api/preview_iframe.php?d=1'
        mock_client_cls.return_value.get_ad_preview.return_value = {'account_id': '123', 'src': src}
        url = reverse('tickets:meta_ads_ad_preview', kwargs={'ad_id': 987})

        first = self.client.get(url)
        second = self.client.get(url)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), {'src': src})
        self.assertEqual(second.json(), {'src': src})
        mock_client_cls.return_value.get_ad_preview.assert_called_once_with('987')

    @patch('tickets.integrations.meta_ads.MetaAdsClient')
    def test_ad_preview_404s_for_ads_in_another_account(self, mock_client_cls):
        mock_client_cls.return_value.get_ad_preview.return_value = {
            'account_id': '999', 'src': 'https://business.facebook.com/ads/api/preview_iframe.php?d=1',
        }

        response = self.client.get(reverse('tickets:meta_ads_ad_preview', kwargs={'ad_id': 987}))

        self.assertEqual(response.status_code, 404)
        self.assertNotIn('src', response.json())

    @patch('tickets.integrations.meta_ads.MetaAdsClient')
    def test_ad_preview_surfaces_meta_errors(self, mock_client_cls):
        mock_client_cls.return_value.get_ad_preview.side_effect = MetaAdsAPIError('Token expired')

        response = self.client.get(reverse('tickets:meta_ads_ad_preview', kwargs={'ad_id': 987}))

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()['error'], 'Token expired')

    def test_ad_preview_requires_connection(self):
        self.org.meta_ads_access_token = ''
        self.org.save(update_fields=['meta_ads_access_token'])

        response = self.client.get(reverse('tickets:meta_ads_ad_preview', kwargs={'ad_id': 987}))

        self.assertEqual(response.status_code, 400)


class _MetaAdsDashboardFixtures:
    """Connected org + logged-in owner, and a mocked Meta client with a small ad pool."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.org = Organization.objects.create(
            name='Meta Org', slug='meta-org',
            meta_ads_access_token='token', meta_ads_account_id='act_123',
        )
        self.user = User.objects.create_user(username='owner', email='owner@example.com', password='pass123')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.force_login(self.user)
        session = self.client.session
        session['_org_id'] = str(self.org.id)
        session.save()
        self.url = reverse('tickets:meta_ads_dashboard')

    def _mock(self, mock_client_cls, ad_rows, keys=None):
        client = mock_client_cls.return_value
        campaign_rows = [_ad_row('1000.00', 100000, 1000, campaign_id='c1', campaign_name='C')]
        client.get_account_insights.side_effect = lambda account_id, level, **kw: (
            campaign_rows if level == 'campaign' else ad_rows
        )
        client.list_campaigns.return_value = []
        client.get_ad_details.return_value = {}
        client.get_ad_creative_keys.return_value = keys or {}
        return client

    ADS = [
        # Big spender, middling ROAS.
        _ad_row('500.00', 50000, 500, purchases=20, value='1000.00', ad_id='1', ad_name='Big', reach='30000'),
        # Efficient mid spender.
        _ad_row('150.00', 15000, 300, purchases=15, value='900.00', ad_id='2', ad_name='Efficient'),
        # Tiny spend, huge ROAS: should be excluded by the default $100 floor.
        _ad_row('5.00', 500, 10, purchases=1, value='200.00', ad_id='3', ad_name='Lucky'),
        # No conversion data at all.
        _ad_row('300.00', 30000, 100, ad_id='4', ad_name='NoPixel'),
        # A copy of "Efficient" (same video) with its own spend.
        _ad_row('60.00', 6000, 120, purchases=3, value='120.00', ad_id='5', ad_name='Efficient - Copy', reach='4000'),
    ]

    def _ids(self, response):
        return [c['id'] for c in response.context['data']['creatives']]


class MetaAdsCreativeRankingTests(_MetaAdsDashboardFixtures, TestCase):
    """Sort / min-spend / grouping of the Top creatives grid."""

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_defaults_rank_by_roas_with_grouping_on(self, mock_client_cls):
        self._mock(mock_client_cls, self.ADS, keys={'2': 'video:B', '5': 'video:B'})
        response = self.client.get(self.url)
        self.assertEqual(response.context['active_sort'], 'roas')
        self.assertTrue(response.context['group_creatives'])
        self.assertEqual(response.context['min_spend_applied'], 100)
        # "Efficient" + its copy merge ($210, 4.86x) and lead; NoPixel has no ROAS.
        self.assertEqual(self._ids(response), ['2', '1', '4'])
        self.assertEqual(response.context['data']['creatives'][0]['ad_count'], 2)
        # Defaults stay out of links.
        self.assertEqual(response.context['creative_query'], '')

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_spend_sort_has_no_floor(self, mock_client_cls):
        self._mock(mock_client_cls, self.ADS)
        response = self.client.get(self.url, {'sort': 'spend', 'group': '0'})
        self.assertEqual(self._ids(response), ['1', '4', '2', '5', '3'])

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_group_toggle_off_and_on(self, mock_client_cls):
        self._mock(mock_client_cls, self.ADS, keys={'2': 'video:B', '5': 'video:B'})
        off = self.client.get(self.url, {'group': '0'})
        # The form submits a hidden 0 plus the checked box's 1; the last value wins.
        on = self.client.get(self.url + '?group=0&group=1')
        self.assertFalse(off.context['group_creatives'])
        self.assertEqual(off.context['creative_query'], 'group=0')
        self.assertTrue(on.context['group_creatives'])
        self.assertContains(on, '<input type="hidden" name="group" value="0">', html=True)

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_roas_sort_applies_default_min_spend_and_puts_unknowns_last(self, mock_client_cls):
        self._mock(mock_client_cls, self.ADS)
        response = self.client.get(self.url, {'sort': 'roas', 'group': '0'})
        # $5 "Lucky" and $60 copy are under the $100 default floor; NoPixel has no ROAS.
        self.assertEqual(self._ids(response), ['2', '1', '4'])
        self.assertEqual(response.context['min_spend_applied'], 100)

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_lowest_min_spend_is_the_pool_floor(self, mock_client_cls):
        self._mock(mock_client_cls, self.ADS)
        response = self.client.get(self.url, {'sort': 'roas', 'min_spend': '25', 'group': '0'})
        # The $60 copy now qualifies; the $5 ad is under the $25 pool floor. Big and
        # the copy tie at 2.00x, broken by spend.
        self.assertEqual(self._ids(response), ['2', '1', '5', '4'])
        self.assertContains(response, 'Ranked from ads that spent at least $25 in this period.')

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_retired_any_min_spend_falls_back_to_default(self, mock_client_cls):
        self._mock(mock_client_cls, self.ADS)
        response = self.client.get(self.url, {'sort': 'roas', 'min_spend': '0'})
        self.assertEqual(response.context['min_spend_applied'], 100)
        self.assertNotContains(response, '>Any<')

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_show_more_adds_a_page_of_creatives(self, mock_client_cls):
        many = [
            _ad_row(f'{1000 - i}.00', 10000, 100, purchases=10, value='500.00', ad_id=str(1000 + i), ad_name=f'Ad {i}')
            for i in range(60)
        ]
        client = self._mock(mock_client_cls, many)
        client.get_ad_details.side_effect = lambda ids: {i: {'image_url': ''} for i in ids}

        first = self.client.get(self.url, {'sort': 'spend', 'group': '0'})
        second = self.client.get(self.url, {'sort': 'spend', 'group': '0', 'shown': '48'})
        last = self.client.get(self.url, {'sort': 'spend', 'group': '0', 'shown': '72'})

        self.assertEqual(len(self._ids(first)), 24)
        self.assertContains(first, 'Show more (24 of 60)')
        self.assertContains(first, 'shown=48')
        self.assertEqual(len(self._ids(second)), 48)
        self.assertEqual(len(self._ids(last)), 60)
        self.assertFalse(last.context['data']['has_more_creatives'])
        # Details for the extra page are fetched; already-cached cards aren't re-fetched.
        fetched = [c.args[0] for c in client.get_ad_details.call_args_list]
        self.assertEqual([len(ids) for ids in fetched], [24, 24, 12])

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_show_more_is_clamped(self, mock_client_cls):
        self._mock(mock_client_cls, self.ADS)
        self.assertEqual(self.client.get(self.url, {'shown': '5000'}).context['data']['has_more_creatives'], False)
        from .services.meta_ads_dashboard import resolve_creatives_shown
        self.assertEqual(resolve_creatives_shown('5000'), 96)
        self.assertEqual(resolve_creatives_shown('junk'), 24)
        self.assertEqual(resolve_creatives_shown('30'), 24)

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_campaign_table_previews_top_20(self, mock_client_cls):
        client = self._mock(mock_client_cls, self.ADS)
        campaigns = [
            _ad_row(f'{100 - i}.00', 1000, 10, campaign_id=f'c{i}', campaign_name=f'Campaign {i}')
            for i in range(25)
        ]
        client.get_account_insights.side_effect = lambda account_id, level, **kw: (
            campaigns if level == 'campaign' else self.ADS
        )
        client.list_campaigns.return_value = [{'id': 'c0', 'status': 'ACTIVE'}]

        response = self.client.get(self.url)

        html = response.content.decode()
        self.assertEqual(html.count('class="campaign-row"'), 20)
        self.assertEqual(html.count('class="campaign-row d-none"'), 5)
        self.assertContains(response, 'Show all 25 campaigns')
        self.assertContains(response, 'data-status="ACTIVE"', count=1)
        self.assertContains(response, 'id="campaignsActiveOnly"')

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_cost_per_purchase_sorts_ascending(self, mock_client_cls):
        self._mock(mock_client_cls, self.ADS)
        response = self.client.get(self.url, {'sort': 'cost_per_purchase', 'min_spend': '25'})
        # $10 (Efficient), $20 (Copy), $25 (Big); NoPixel (no purchases) last.
        self.assertEqual(self._ids(response), ['2', '5', '1', '4'])

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_unknown_sort_and_min_spend_fall_back(self, mock_client_cls):
        self._mock(mock_client_cls, self.ADS)
        response = self.client.get(self.url, {'sort': 'bogus', 'min_spend': '7'})
        self.assertEqual(response.context['active_sort'], 'roas')
        response = self.client.get(self.url, {'sort': 'ctr', 'min_spend': '7'})
        self.assertEqual(response.context['min_spend_applied'], 100)

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_grouping_merges_ads_sharing_a_creative(self, mock_client_cls):
        self._mock(mock_client_cls, self.ADS, keys={
            '1': 'video:A', '2': 'video:B', '3': 'video:C', '4': 'video:D', '5': 'video:B',
        })
        response = self.client.get(self.url, {'group': '1', 'sort': 'spend'})

        creatives = response.context['data']['creatives']
        self.assertEqual([c['id'] for c in creatives], ['1', '4', '2', '3'])
        grouped = creatives[2]
        # Represented by its top spender; metrics summed across both ads.
        self.assertEqual(grouped['name'], 'Efficient')
        self.assertEqual(grouped['ad_count'], 2)
        self.assertEqual([m['id'] for m in grouped['ads']], ['2', '5'])
        self.assertEqual(grouped['spend'], Decimal('210.00'))
        self.assertEqual(grouped['purchases'], 18)
        self.assertEqual(grouped['roas'], Decimal('4.86'))
        # Reach isn't additive across ads.
        self.assertIsNone(grouped['reach'])
        self.assertEqual(creatives[0]['reach'], 30000)
        self.assertContains(response, 'Ads using this creative')
        self.assertContains(response, 'Efficient - Copy')

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_min_spend_applies_to_group_totals(self, mock_client_cls):
        # Two $60 copies: each under $100, together over it.
        ads = [
            _ad_row('60.00', 6000, 60, purchases=6, value='600.00', ad_id='1', ad_name='A'),
            _ad_row('60.00', 6000, 60, purchases=6, value='600.00', ad_id='2', ad_name='A - Copy'),
        ]
        self._mock(mock_client_cls, ads, keys={'1': 'video:A', '2': 'video:A'})

        ungrouped = self.client.get(self.url, {'sort': 'roas', 'group': '0'})
        grouped = self.client.get(self.url, {'sort': 'roas', 'group': '1'})

        self.assertEqual(self._ids(ungrouped), [])
        self.assertContains(ungrouped, 'No creatives spent at least')
        self.assertEqual(self._ids(grouped), ['1'])

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_grouping_falls_back_to_ungrouped_when_keys_fail(self, mock_client_cls):
        client = self._mock(mock_client_cls, self.ADS)
        client.get_ad_creative_keys.side_effect = MetaAdsAPIError('rate limited')
        response = self.client.get(self.url, {'group': '1', 'sort': 'spend'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self._ids(response)), 5)

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_changing_controls_reuses_cached_insights_and_keys(self, mock_client_cls):
        client = self._mock(mock_client_cls, self.ADS, keys={'1': 'video:A'})

        self.client.get(self.url, {'group': '1'})
        self.client.get(self.url, {'group': '1', 'sort': 'roas'})
        self.client.get(self.url, {'sort': 'purchases'})

        # One campaign + one ad Insights call total; keys fetched once.
        self.assertEqual(client.get_account_insights.call_count, 2)
        self.assertEqual(client.get_ad_creative_keys.call_count, 1)

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_range_links_keep_creative_controls(self, mock_client_cls):
        self._mock(mock_client_cls, self.ADS)
        response = self.client.get(self.url, {'sort': 'purchases', 'min_spend': '250', 'group': '0'})
        self.assertContains(response, 'href="?range=last_7d&amp;sort=purchases&amp;min_spend=250&amp;group=0"')

    def test_creative_key_prefers_video_then_image_then_creative_id(self):
        from .services.meta_ads import _creative_key
        self.assertEqual(_creative_key({'id': 'c', 'object_story_spec': {'video_data': {'video_id': 'v'}}}), 'video:v')
        self.assertEqual(_creative_key({'id': 'c', 'asset_feed_spec': {'videos': [{'video_id': 'fv'}]}}), 'video:fv')
        self.assertEqual(_creative_key({'id': 'c', 'object_story_spec': {'link_data': {'image_hash': 'h'}}}), 'image:h')
        self.assertEqual(_creative_key({'id': 'c', 'asset_feed_spec': {'images': [{'hash': 'fh'}]}}), 'image:fh')
        self.assertEqual(_creative_key({'id': 'c'}), 'creative:c')
        self.assertEqual(_creative_key({}), '')

    @patch('tickets.services.meta_ads.requests.get')
    def test_get_ad_creative_keys_uses_filtered_ads_edge_then_falls_back(self, mock_get):
        import json as _json

        def respond(url, params, timeout):
            resp = MagicMock(status_code=200)
            video = lambda i: {'creative': {'object_story_spec': {'video_data': {'video_id': f'v{i}'}}}}
            if '/act_123/ads' in url:
                wanted = _json.loads(params['filtering'])[0]['value']
                # The /ads edge omits archived ad '7'.
                resp.json.return_value = {'data': [{'id': i, **video(i)} for i in wanted if i != '7']}
            else:
                resp.json.return_value = {i: video(i) for i in params['ids'].split(',')}
            return resp
        mock_get.side_effect = respond

        keys = MetaAdsClient('token').get_ad_creative_keys('act_123', [str(i) for i in range(200)])

        urls = [c.args[0] for c in mock_get.call_args_list]
        self.assertEqual(sum('/act_123/ads' in u for u in urls), 2)  # 150 + 50
        self.assertEqual(len(urls), 3)  # + one direct lookup for the archived ad
        self.assertEqual(mock_get.call_args_list[-1].kwargs['params']['ids'], '7')
        self.assertEqual(keys['7'], 'video:v7')
        self.assertEqual(keys['199'], 'video:v199')


class MetaAdsRateLimitTests(_MetaAdsDashboardFixtures, TestCase):
    """Rate-limit handling: friendly message, and last-good data when we have it."""

    def _rate_limit(self):
        return MetaAdsAPIError('too many calls', code=4, subcode=1504022)

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_rate_limit_without_backup_shows_wait_message(self, mock_client_cls):
        client = self._mock(mock_client_cls, self.ADS)
        client.get_account_insights.side_effect = self._rate_limit()

        response = self.client.get(self.url)

        self.assertContains(response, 'Meta is limiting how often Cue can pull ad data')
        self.assertNotContains(response, 'Check connection')

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_rate_limited_refresh_serves_last_good_data(self, mock_client_cls):
        client = self._mock(mock_client_cls, self.ADS)
        self.client.get(self.url)  # populates fresh + backup caches

        client.get_account_insights.side_effect = self._rate_limit()
        response = self.client.get(self.url, {'refresh': '1', 'sort': 'spend', 'group': '0'})

        self.assertEqual(len(response.context['data']['creatives']), 5)
        self.assertIsNotNone(response.context['data']['stale_fetched_at'])
        self.assertContains(response, 'so this is data from')

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_other_errors_do_not_fall_back(self, mock_client_cls):
        client = self._mock(mock_client_cls, self.ADS)
        self.client.get(self.url)

        client.get_account_insights.side_effect = MetaAdsAPIError('Token expired', code=190)
        response = self.client.get(self.url, {'refresh': '1'})

        self.assertIsNone(response.context['data'])
        self.assertContains(response, 'Check connection')


class MetaAdsClientBatchingTests(TestCase):
    @patch('tickets.services.meta_ads.requests.get')
    def test_get_ad_details_batches_50_ids_per_call(self, mock_get):
        def respond(url, params, timeout):
            resp = MagicMock(status_code=200)
            resp.json.return_value = {i: {'creative': {}} for i in params['ids'].split(',')}
            return resp
        mock_get.side_effect = respond

        details = MetaAdsClient('token').get_ad_details([str(i) for i in range(96)])

        self.assertEqual(len(details), 96)
        self.assertEqual(sorted(len(c.kwargs['params']['ids'].split(',')) for c in mock_get.call_args_list), [46, 50])

    @patch('tickets.services.meta_ads.requests.get')
    def test_insights_min_spend_becomes_a_meta_filter(self, mock_get):
        import json as _json
        response = MagicMock(status_code=200)
        response.json.return_value = {'data': []}
        mock_get.return_value = response

        MetaAdsClient('token').get_account_insights('act_1', level='ad', fields='spend', date_preset='last_7d', min_spend=25)

        self.assertEqual(
            _json.loads(mock_get.call_args.kwargs['params']['filtering']),
            [{'field': 'spend', 'operator': 'GREATER_THAN_OR_EQUAL', 'value': 25.0}],
        )


def _daily_row(day, ad_id, spend, purchases=None, value=None, campaign_id='c1', **extra):
    row = {
        'date_start': day.isoformat(), 'ad_id': ad_id, 'ad_name': f'Ad {ad_id}',
        'adset_name': 'Set', 'campaign_id': campaign_id, 'campaign_name': f'Campaign {campaign_id}',
        'account_currency': 'USD', 'spend': spend, 'impressions': '1000', 'inline_link_clicks': '20',
        **extra,
    }
    if purchases is not None:
        row['actions'] = [{'action_type': 'purchase', 'value': str(purchases)}]
        row['action_values'] = [{'action_type': 'purchase', 'value': str(value)}]
    return row


class MetaAdsInsightsSyncTests(TestCase):
    def setUp(self):
        from datetime import date
        self.today = date(2026, 11, 17)
        self.org = Organization.objects.create(
            name='Meta Org', slug='meta-org',
            meta_ads_access_token='token', meta_ads_account_id='act_123',
        )
        self.client_mock = MagicMock()

    def _sync(self):
        from .services.meta_ads_sync import MetaAdsInsightsSync
        return MetaAdsInsightsSync(self.org, client=self.client_mock)

    def test_sync_recent_replaces_trailing_window_and_marks_coverage(self):
        from datetime import date, timedelta
        from .models import MetaAdDailyInsight
        old_day = date(2026, 9, 1)  # outside the 28-day window: must survive
        MetaAdDailyInsight.objects.create(organization=self.org, ad_account_id='act_123', date=old_day, ad_id='9', spend=5)
        MetaAdDailyInsight.objects.create(organization=self.org, ad_account_id='act_123', date=self.today, ad_id='1', spend=1)
        self.client_mock.get_account_insights.return_value = [
            _daily_row(self.today, '1', '40.00', purchases=2, value='90.00'),
            _daily_row(self.today - timedelta(days=1), '1', '30.00'),
        ]

        count = self._sync().sync_recent(today=self.today)

        kwargs = self.client_mock.get_account_insights.call_args.kwargs
        self.assertEqual(kwargs['time_increment'], 1)
        self.assertEqual(kwargs['time_range'], (self.today - timedelta(days=27), self.today))
        self.assertEqual(count, 2)
        rows = MetaAdDailyInsight.objects.filter(organization=self.org)
        self.assertEqual(rows.count(), 3)
        latest = rows.get(date=self.today)
        self.assertEqual((latest.spend, latest.purchases, latest.purchase_value), (Decimal('40.00'), 2, Decimal('90.00')))
        self.assertIsNone(rows.get(date=self.today - timedelta(days=1)).purchases)
        self.assertTrue(rows.filter(date=old_day).exists())
        self.org.refresh_from_db()
        self.assertEqual(self.org.meta_ads_synced_since, self.today - timedelta(days=27))
        self.assertIsNotNone(self.org.meta_ads_synced_at)

    def test_backfill_walks_back_a_month_at_a_time_to_the_floor(self):
        from datetime import date
        self.client_mock.get_account_insights.return_value = []
        self.org.meta_ads_synced_since = date(2026, 10, 21)
        sync = self._sync()

        sync.backfill_previous_month(today=self.today)
        self.assertEqual(self.client_mock.get_account_insights.call_args.kwargs['time_range'],
                         (date(2026, 10, 1), date(2026, 10, 20)))
        sync.backfill_previous_month(today=self.today)
        self.assertEqual(self.client_mock.get_account_insights.call_args.kwargs['time_range'],
                         (date(2026, 9, 1), date(2026, 9, 30)))
        self.assertFalse(sync.is_backfilled(today=self.today))

        self.org.meta_ads_synced_since = date(2023, 12, 1)
        sync.backfill_previous_month(today=self.today)
        self.assertEqual(self.org.meta_ads_synced_since, date(2023, 11, 1))
        self.assertTrue(sync.is_backfilled(today=self.today))
        self.assertIsNone(sync.backfill_previous_month(today=self.today))

    def test_backfill_needs_an_initial_sync(self):
        self.assertIsNone(self._sync().backfill_previous_month(today=self.today))
        self.client_mock.get_account_insights.assert_not_called()


class MetaAdsSyncTaskTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.org = Organization.objects.create(
            name='Meta Org', slug='meta-org',
            meta_ads_access_token='token', meta_ads_account_id='act_123',
        )

    @patch('tickets.tasks.backfill_meta_ads_insights_task.apply_async')
    @patch('tickets.services.meta_ads_sync.MetaAdsClient')
    def test_sync_starts_one_backfill_chain(self, mock_client_cls, mock_backfill):
        from .tasks import sync_meta_ads_insights_task
        mock_client_cls.return_value.get_account_insights.return_value = []

        sync_meta_ads_insights_task.apply(args=[str(self.org.pk)])
        sync_meta_ads_insights_task.apply(args=[str(self.org.pk)])

        self.assertEqual(mock_backfill.call_count, 1)  # lock held by the running chain

    @patch('tickets.tasks.backfill_meta_ads_insights_task.apply_async')
    @patch('tickets.services.meta_ads_sync.MetaAdsClient')
    def test_refresh_sync_skips_backfill(self, mock_client_cls, mock_backfill):
        from .tasks import sync_meta_ads_insights_task
        mock_client_cls.return_value.get_account_insights.return_value = []
        sync_meta_ads_insights_task.apply(args=[str(self.org.pk)], kwargs={'start_backfill': False})
        mock_backfill.assert_not_called()

    @patch('tickets.services.meta_ads_sync.MetaAdsClient')
    def test_rate_limited_sync_retries(self, mock_client_cls):
        from celery.exceptions import Retry
        from .tasks import sync_meta_ads_insights_task
        mock_client_cls.return_value.get_account_insights.side_effect = MetaAdsAPIError('slow down', code=4)
        with patch.object(sync_meta_ads_insights_task, 'retry', side_effect=Retry()) as mock_retry:
            with self.assertRaises(Retry):
                sync_meta_ads_insights_task.run(str(self.org.pk))
        mock_retry.assert_called_once()

    def test_unconnected_org_is_skipped(self):
        from .tasks import sync_meta_ads_insights_task
        self.org.meta_ads_account_id = ''
        self.org.save()
        self.assertEqual(sync_meta_ads_insights_task.apply(args=[str(self.org.pk)]).get(), 'not_connected')

    @patch('tickets.tasks.backfill_meta_ads_insights_task.apply_async')
    @patch('tickets.services.meta_ads_sync.MetaAdsClient')
    def test_backfill_chains_until_floor_then_releases_lock(self, mock_client_cls, mock_next):
        from datetime import date
        from django.core.cache import cache
        from django.utils import timezone
        from .services.meta_ads_dashboard import insights_history_floor
        from .tasks import _meta_ads_backfill_lock_key, backfill_meta_ads_insights_task
        mock_client_cls.return_value.get_account_insights.return_value = []
        floor = insights_history_floor(timezone.localdate())
        cache.set(_meta_ads_backfill_lock_key(str(self.org.pk)), 1)

        self.org.meta_ads_synced_since = date(2026, 10, 21)
        self.org.save()
        backfill_meta_ads_insights_task.apply(args=[str(self.org.pk)])
        self.assertEqual(mock_next.call_count, 1)

        self.org.meta_ads_synced_since = floor.replace(month=floor.month % 12 + 1) if floor.month < 12 else floor.replace(year=floor.year + 1, month=1)
        self.org.save()
        result = backfill_meta_ads_insights_task.apply(args=[str(self.org.pk)]).get()
        self.assertEqual(result, 'done')
        self.assertEqual(mock_next.call_count, 1)
        self.assertIsNone(cache.get(_meta_ads_backfill_lock_key(str(self.org.pk))))

    @patch('tickets.management.commands.sync_meta_ads_insights.sync_meta_ads_insights_task.delay')
    def test_command_enqueues_connected_orgs(self, mock_delay):
        from django.core.management import call_command
        Organization.objects.create(name='Other', slug='other')  # not connected
        call_command('sync_meta_ads_insights', stdout=MagicMock())
        mock_delay.assert_called_once_with(str(self.org.pk))


class MetaAdsSyncedDashboardTests(_MetaAdsDashboardFixtures, TestCase):
    """Dashboard reads synced daily rows when they cover the range."""

    def setUp(self):
        super().setUp()
        from datetime import timedelta
        from django.utils import timezone
        from .models import MetaAdDailyInsight
        self.today = timezone.localdate()
        self.org.meta_ads_synced_since = self.today - timedelta(days=60)
        self.org.meta_ads_synced_at = timezone.now()
        self.org.save()

        def add(days_ago, ad_id, spend, purchases=None, value=None, campaign_id='c1'):
            MetaAdDailyInsight.objects.create(
                organization=self.org, ad_account_id='act_123', date=self.today - timedelta(days=days_ago),
                ad_id=ad_id, ad_name=f'Ad {ad_id}', campaign_id=campaign_id, campaign_name=f'Campaign {campaign_id}',
                currency='USD', spend=Decimal(spend), impressions=1000, link_clicks=20,
                purchases=purchases, purchase_value=Decimal(value) if value is not None else None,
            )
        add(1, '1', '100.00', 5, '400.00')
        add(2, '1', '50.00', 1, '50.00')
        add(3, '2', '200.00', campaign_id='c2')  # no conversion data
        add(40, '1', '999.00', 99, '9999.00')  # outside last_30d
        add(1, '3', '10.00', 1, '100.00')  # under the $25 pool floor
        # Another account's row must never leak in.
        MetaAdDailyInsight.objects.create(
            organization=self.org, ad_account_id='act_999', date=self.today - timedelta(days=1),
            ad_id='x', spend=Decimal('5000'), impressions=1,
        )

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_sums_synced_rows_without_insights_calls(self, mock_client_cls):
        client = self._mock(mock_client_cls, [])
        client.list_campaigns.return_value = [{'id': 'c1', 'status': 'ACTIVE'}]

        response = self.client.get(self.url, {'range': 'last_30d', 'sort': 'spend', 'group': '0'})

        client.get_account_insights.assert_not_called()
        data = response.context['data']
        self.assertEqual(data['source'], 'synced')
        self.assertEqual(data['totals']['spend'], Decimal('360.00'))
        self.assertEqual(data['totals']['purchases'], 7)
        campaigns = {c['id']: c for c in data['campaigns']}
        self.assertEqual(campaigns['c1']['spend'], Decimal('160.00'))
        self.assertEqual(campaigns['c1']['status'], 'ACTIVE')
        self.assertIsNone(campaigns['c2']['purchases'])  # every day lacked conversion data
        ad = {c['id']: c for c in data['creatives']}
        self.assertEqual(set(ad), {'1', '2'})  # ad 3 under the $25 floor
        self.assertEqual(ad['1']['spend'], Decimal('150.00'))
        self.assertEqual(ad['1']['roas'], Decimal('3.00'))
        self.assertIsNone(ad['1']['reach'])
        self.assertContains(response, 'Synced')

    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_falls_back_to_live_outside_coverage_or_when_stale(self, mock_client_cls):
        from datetime import timedelta
        from django.utils import timezone
        client = self._mock(mock_client_cls, self.ADS)

        response = self.client.get(self.url, {'range': 'last_90d'})  # older than synced_since
        self.assertEqual(response.context['data']['source'], 'live')
        self.assertContains(response, 'Live from Meta')

        self.org.meta_ads_synced_at = timezone.now() - timedelta(days=2)
        self.org.save()
        response = self.client.get(self.url, {'range': 'last_7d'})
        self.assertEqual(response.context['data']['source'], 'live')
        self.assertTrue(client.get_account_insights.called)

    @patch('tickets.tasks.sync_meta_ads_insights_task.delay')
    @patch('tickets.services.meta_ads_dashboard.MetaAdsClient')
    def test_refresh_enqueues_a_sync(self, mock_client_cls, mock_delay):
        self._mock(mock_client_cls, [])
        response = self.client.get(self.url, {'range': 'last_7d', 'refresh': '1'})
        mock_delay.assert_called_once_with(str(self.org.pk), start_backfill=False)
        self.assertContains(response, 'Syncing the latest numbers from Meta')

    def test_preset_bounds_match_meta_semantics(self):
        from datetime import date
        from .services.meta_ads_dashboard import insights_history_floor, preset_date_range
        today = date(2026, 11, 17)
        self.assertEqual(preset_date_range('last_7d').bounds(today), (date(2026, 11, 10), date(2026, 11, 16)))
        self.assertEqual(preset_date_range('maximum').bounds(today), (insights_history_floor(today), today))


class MetaAdsSyncLifecycleTests(_MetaAdsDashboardFixtures, TestCase):
    def setUp(self):
        super().setUp()
        from datetime import date
        from django.utils import timezone
        from .models import MetaAdDailyInsight
        self.org.meta_ads_synced_since = date(2026, 1, 1)
        self.org.meta_ads_synced_at = timezone.now()
        self.org.save()
        MetaAdDailyInsight.objects.create(organization=self.org, ad_account_id='act_123', date=date(2026, 5, 1), ad_id='1', spend=1)

    def _rows(self):
        from .models import MetaAdDailyInsight
        return MetaAdDailyInsight.objects.filter(organization=self.org).count()

    @patch('tickets.integrations.meta_ads.MetaAdsClient')
    def test_reselecting_same_account_keeps_history(self, mock_client_cls):
        mock_client_cls.return_value.list_ad_accounts.return_value = [{'id': 'act_123', 'name': 'Main'}]
        self.org.meta_ads_account_id = ''  # as left by an OAuth reconnect
        self.org.save()
        self.client.post(reverse('tickets:meta_ads_select_account'), {'account_id': 'act_123'})
        self.org.refresh_from_db()
        self.assertEqual(self._rows(), 1)
        self.assertIsNotNone(self.org.meta_ads_synced_since)

    @patch('tickets.integrations.meta_ads.MetaAdsClient')
    def test_switching_account_resets_history(self, mock_client_cls):
        mock_client_cls.return_value.list_ad_accounts.return_value = [{'id': 'act_456', 'name': 'Other'}]
        self.client.post(reverse('tickets:meta_ads_select_account'), {'account_id': 'act_456'})
        self.org.refresh_from_db()
        self.assertEqual(self._rows(), 0)
        self.assertIsNone(self.org.meta_ads_synced_since)

    def test_disconnect_deletes_synced_data(self):
        self.client.post(reverse('tickets:meta_ads_disconnect'))
        self.org.refresh_from_db()
        self.assertEqual(self._rows(), 0)
        self.assertIsNone(self.org.meta_ads_synced_at)


class EventAdsInsightsTests(TestCase):
    """Marketing tab → Ads sub-view: per-event metrics from synced daily rows."""

    def setUp(self):
        # No access token: keeps event_detail's live Meta refresh a no-op.
        self.org = Organization.objects.create(name='Ads Org', slug='ads-org', meta_ads_account_id='act_1')
        self.other_org = Organization.objects.create(name='Other Org', slug='other-org')
        self.user = User.objects.create_user(username='ads-owner', email='ads@example.com', password='pass123')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        venue = Venue.objects.create(organization=self.org, name='Venue', city='Oakland')
        self.event = Event.objects.create(
            organization=self.org, venue=venue, name='Event', start_date=date(2026, 6, 1),
        )
        self.client.force_login(self.user)
        session = self.client.session
        session['_org_id'] = str(self.org.id)
        session.save()
        self.url = reverse('tickets:event_detail', kwargs={'event_id': self.event.id})

    def _link(self, campaign_id, name='Campaign'):
        return EventExpense.objects.create(
            event=self.event, category='marketing', description=name, amount=Decimal('0.00'),
            source='meta_ads', external_id=campaign_id, external_metadata={'campaign_name': name},
        )

    def _row(self, campaign_id, day, ad_id='ad1', org=None, **metrics):
        defaults = {'spend': Decimal('10.00'), 'impressions': 1000, 'link_clicks': 20}
        defaults.update(metrics)
        return MetaAdDailyInsight.objects.create(
            organization=org or self.org, ad_account_id='act_1', date=day, ad_id=ad_id,
            campaign_id=campaign_id, campaign_name=f'Name {campaign_id}', currency='USD', **defaults,
        )

    def _insights(self):
        from .views import _get_event_ads_insights
        expenses = list(EventExpense.objects.filter(event=self.event, source='meta_ads'))
        return _get_event_ads_insights(self.event, expenses)

    def test_hidden_without_linked_campaigns(self):
        result = self._insights()
        self.assertFalse(result['show'])
        response = self.client.get(self.url)
        self.assertNotContains(response, 'data-marketing-view="ads"')

    def test_linked_campaign_without_synced_rows_shows_empty_state(self):
        self._link('cmp_1')
        result = self._insights()
        self.assertTrue(result['show'])
        self.assertFalse(result['has_data'])
        response = self.client.get(self.url)
        self.assertContains(response, 'data-marketing-view="ads"')
        self.assertContains(response, 'No synced data yet for the linked campaigns.')

    def test_aggregates_only_linked_campaigns_in_this_org(self):
        self._link('cmp_1')
        self._link('cmp_2')
        self._row('cmp_1', date(2026, 5, 1), purchases=2, purchase_value=Decimal('60.00'))
        self._row('cmp_1', date(2026, 5, 2), ad_id='ad2', spend=Decimal('20.00'), purchases=1,
                  purchase_value=Decimal('30.00'))
        self._row('cmp_2', date(2026, 5, 2), ad_id='ad3', spend=Decimal('5.00'))
        self._row('cmp_unlinked', date(2026, 5, 2), ad_id='ad4', spend=Decimal('999.00'))
        self._row('cmp_1', date(2026, 5, 2), ad_id='ad5', org=self.other_org, spend=Decimal('999.00'))

        result = self._insights()

        totals = result['totals']
        self.assertTrue(result['has_data'])
        self.assertEqual(totals['spend'], Decimal('35.00'))
        self.assertEqual(totals['impressions'], 3000)
        self.assertEqual(totals['link_clicks'], 60)
        self.assertEqual(totals['purchases'], 3)
        self.assertEqual(totals['ctr'], Decimal('2.00'))
        self.assertEqual(totals['cpm'], Decimal('11.67'))
        self.assertEqual(totals['roas'], Decimal('2.57'))
        self.assertEqual([c['name'] for c in result['campaigns']], ['Name cmp_1', 'Name cmp_2'])
        self.assertEqual(result['campaigns'][0]['spend'], Decimal('30.00'))
        self.assertIsNone(result['campaigns'][1]['purchases'])
        # Daily raw totals feed the chart's metric toggle; ratios are derived client-side.
        self.assertEqual(
            json.loads(result['daily_json']),
            [
                {'date': '2026-05-01', 'days_before': (self.event.start_date - date(2026, 5, 1)).days,
                 'spend': 10.0, 'impressions': 1000, 'clicks': 20,
                 'purchases': 2, 'purchase_value': 60.0, 'results': None},
                {'date': '2026-05-02', 'days_before': (self.event.start_date - date(2026, 5, 2)).days,
                 'spend': 25.0, 'impressions': 2000, 'clicks': 40,
                 'purchases': 1, 'purchase_value': 30.0, 'results': None},
            ],
        )

    def test_daily_series_keeps_missing_conversions_as_null(self):
        # A day with no pixel data must not be plotted as zero purchases.
        self._link('cmp_1')
        self._row('cmp_1', date(2026, 5, 1), purchases=4, purchase_value=Decimal('80.00'))
        self._row('cmp_1', date(2026, 5, 2), ad_id='ad2')
        daily = json.loads(self._insights()['daily_json'])
        self.assertEqual(daily[0]['purchases'], 4)
        self.assertIsNone(daily[1]['purchases'])
        self.assertIsNone(daily[1]['purchase_value'])

    def test_renders_metrics_in_marketing_tab(self):
        self._link('cmp_1')
        self._row('cmp_1', date(2026, 5, 1), spend=Decimal('1234.50'))
        response = self.client.get(self.url)
        self.assertContains(response, 'data-marketing-section="ads"')
        self.assertContains(response, '$1,234.50')
        self.assertContains(response, 'Campaign breakdown')
        self.assertContains(response, 'id="adsDailyChart"')
        # The event has no orders, so Chart.js must be loaded for the ads chart alone.
        self.assertContains(response, 'chart.umd.min.js')
        for metric in ('spend', 'impressions', 'clicks', 'ctr', 'cpc', 'cpm'):
            self.assertContains(response, f'data-ads-metric="{metric}"')
        # No pixel data: conversion-based views are hidden rather than flat at zero.
        for metric in ('results', 'cost_per_result', 'roas'):
            self.assertNotContains(response, f'data-ads-metric="{metric}"')

    def test_results_metric_toggles_shown_with_conversion_data(self):
        self._link('cmp_1')
        self._row('cmp_1', date(2026, 5, 1), purchases=3, purchase_value=Decimal('90.00'), results=3)
        response = self.client.get(self.url)
        # results and cost_per_result replace purchases/cost_per_purchase;
        # roas is shown when purchase_value data exists.
        for metric in ('results', 'cost_per_result', 'roas'):
            self.assertContains(response, f'data-ads-metric="{metric}"')
        # purchases toggle is no longer rendered (results covers both).
        self.assertNotContains(response, 'data-ads-metric="purchases"')
