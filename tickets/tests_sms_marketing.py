"""Tests for native marketing SMS: recipient resolution, sending, scheduling,
webhooks, views, and analytics."""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    Organization, UserProfile, Customer, CustomerTag,
    SMSCampaign, SMSMessageRecipient, PhoneSuppression,
    Venue, Market, Event, CSVFormat, UploadedFile, TicketOrder,
)


def make_customer(org, email, phone='+13105550000', opt_in=True, **kwargs):
    # Default to a VIP segment so compose-flow tests (which target rfm_segment)
    # resolve a non-empty audience; explicit rfm_segment kwargs still override.
    kwargs.setdefault('rfm_segment', 'VIP')
    return Customer.objects.create(
        organization=org, email=email, name=kwargs.pop('name', email.split('@')[0]),
        phone=phone, sms_opt_in=opt_in, **kwargs,
    )


@override_settings(E2E_TEST_MODE=True, SMS_CAMPAIGN_MAX_RECIPIENTS=5000)
class SMSCampaignAudienceTests(TestCase):
    """Audience resolution now lives inline on SMSCampaign.materialize()."""
    def setUp(self):
        self.org = Organization.objects.create(name='Org A', slug='org-a', sms_marketing_enabled=True)

    def _campaign(self, **kwargs):
        # Unsaved is fine — materialize() only reads org + criteria fields.
        return SMSCampaign(organization=self.org, name='C', body='hi', **kwargs)

    def test_only_opted_in_with_phone(self):
        make_customer(self.org, 'a@x.com', '+13105550001', opt_in=True)
        make_customer(self.org, 'b@x.com', '+13105550002', opt_in=False)   # not opted in
        make_customer(self.org, 'c@x.com', '', opt_in=True)                 # no phone
        c = self._campaign(filter_criteria={'min_ltv': '0'})
        phones = {r['phone'] for r in c.materialize(self.org)}
        self.assertEqual(phones, {'+13105550001'})

    def test_excludes_suppressed_global_and_org(self):
        make_customer(self.org, 'a@x.com', '+13105550001')
        make_customer(self.org, 'b@x.com', '+13105550002')
        PhoneSuppression.objects.create(phone='+13105550001', organization=None)        # global
        PhoneSuppression.objects.create(phone='+13105550002', organization=self.org)    # org
        c = self._campaign(filter_criteria={'min_ltv': '0'})
        self.assertEqual(c.materialize(self.org), [])

    def test_dedupe_by_phone(self):
        # Same number, two customer rows (different email) -> one recipient.
        make_customer(self.org, 'a@x.com', '3105550009')
        make_customer(self.org, 'b@x.com', '+13105550009')
        c = self._campaign(filter_criteria={'min_ltv': '0'})
        self.assertEqual(len(c.materialize(self.org)), 1)

    def test_manual_include_only_works(self):
        cust = make_customer(self.org, 'a@x.com', '+13105550001')
        c = self._campaign(filter_criteria={}, manual_include_ids=[str(cust.id)])
        self.assertEqual(len(c.materialize(self.org)), 1)

    def test_manual_exclude_wins(self):
        make_customer(self.org, 'a@x.com', '+13105550001', rfm_segment='VIP')
        c2 = make_customer(self.org, 'b@x.com', '+13105550002', rfm_segment='VIP')
        c = self._campaign(filter_criteria={'rfm_segment': ['VIP']}, manual_exclude_ids=[str(c2.id)])
        phones = {r['phone'] for r in c.materialize(self.org)}
        self.assertEqual(phones, {'+13105550001'})

    def test_empty_criteria_and_no_includes_is_empty(self):
        make_customer(self.org, 'a@x.com', '+13105550001')
        c = self._campaign(filter_criteria={}, manual_include_ids=[])
        self.assertEqual(c.materialize(self.org), [])

    def test_tags_audience(self):
        tag = CustomerTag.objects.create(organization=self.org, name='VIP')
        c1 = make_customer(self.org, 'a@x.com', '+13105550001')
        c1.tags.add(tag)
        make_customer(self.org, 'b@x.com', '+13105550002')  # untagged
        c = self._campaign(filter_criteria={'tag_ids': [str(tag.id)]})
        phones = {r['phone'] for r in c.materialize(self.org)}
        self.assertEqual(phones, {'+13105550001'})

    def test_cap_is_enforced(self):
        for i in range(5):
            make_customer(self.org, f'c{i}@x.com', f'+1310555100{i}')
        c = self._campaign(filter_criteria={'min_ltv': '0'})
        self.assertEqual(len(c.materialize(self.org, cap=3)), 3)

    def test_audience_summary(self):
        tag = CustomerTag.objects.create(organization=self.org, name='Press')
        c = self._campaign(filter_criteria={'tag_ids': [str(tag.id)], 'rfm_segment': ['VIP']})
        summary = c.audience_summary(self.org)
        self.assertIn('Segments: VIP', summary)
        self.assertIn('Tags: Press', summary)
        empty = self._campaign(filter_criteria={}).audience_summary(self.org)
        self.assertEqual(empty, 'No audience')

    def test_suppression_is_suppressed_helper(self):
        PhoneSuppression.objects.create(phone='+13105550001', organization=None)
        self.assertTrue(PhoneSuppression.is_suppressed('+13105550001', self.org))
        self.assertFalse(PhoneSuppression.is_suppressed('+13105559999', self.org))

    # T11: audience_summary handles __none__ sentinel and invalid/list market criteria
    def test_audience_summary_market_none_sentinel(self):
        from .services.customer_filters import NO_MARKET_VALUE
        c = self._campaign(filter_criteria={'rfm_segment': ['VIP'], 'market_id': NO_MARKET_VALUE})
        self.assertIn('No market', c.audience_summary(self.org))

    def test_audience_summary_market_list_coercion(self):
        from tickets.models import Venue, Market, Event
        venue = Venue.objects.create(organization=self.org, name='H', city='A')
        mkt = Market.objects.create(
            organization=self.org, name='Austin', geography_level='city', geography_value='Austin',
        )
        c = self._campaign(filter_criteria={'rfm_segment': ['VIP'], 'market_ids': [str(mkt.id)]})
        self.assertIn('Markets: Austin', c.audience_summary(self.org))

    def test_audience_summary_invalid_market_id_ignored(self):
        c = self._campaign(filter_criteria={'rfm_segment': ['VIP'], 'market_id': 'not-a-uuid'})
        # Should not raise; invalid UUID is silently filtered out
        summary = c.audience_summary(self.org)
        self.assertNotIn('Markets:', summary)


class FilterCustomersRegressionTests(TestCase):
    """The customer_list view was refactored onto filter_customers — make sure
    its segment/tag/search behavior is unchanged."""
    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Org', slug='org-reg', sms_marketing_enabled=True)
        self.user = User.objects.create_user('u', 'u@test.com', 'pw')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='u@test.com', password='pw')
        self.client.get(reverse('tickets:home'))
        self.tag = CustomerTag.objects.create(organization=self.org, name='VIP')
        self.vip = make_customer(self.org, 'vip@x.com', name='Vippy', rfm_segment='VIP')
        self.vip.tags.add(self.tag)
        self.reg = make_customer(self.org, 'reg@x.com', name='Reggie', rfm_segment='Loyal')
        self.no_market_vip = make_customer(self.org, 'nomarket@x.com', name='No Market', rfm_segment='Lapsed')
        self.venue = Venue.objects.create(organization=self.org, name='Austin Hall', city='Austin')
        self.other_venue = Venue.objects.create(organization=self.org, name='Seattle Hall', city='Seattle')
        self.market = Market.objects.create(
            organization=self.org, name='Austin', geography_level='city', geography_value='Austin',
        )
        self.other_market = Market.objects.create(
            organization=self.org, name='Seattle', geography_level='city', geography_value='Seattle',
        )
        self.event = Event.objects.create(
            organization=self.org, name='Austin Show', venue=self.venue,
            market=self.market, start_date=date(2026, 1, 1),
        )
        self.other_event = Event.objects.create(
            organization=self.org, name='Seattle Show', venue=self.other_venue,
            market=self.other_market, start_date=date(2026, 1, 2),
        )
        self.unassigned_event = Event.objects.create(
            organization=self.org, name='Unassigned Show', venue=self.venue,
            start_date=date(2026, 1, 3),
        )
        self.fmt = CSVFormat.objects.create(organization=self.org, name='F', column_mapping={'order_number': 'O'})
        self.upload = UploadedFile.objects.create(
            organization=self.org, csv_format=self.fmt, filename='f.csv', status='completed',
        )
        for customer, event, order_number, total in (
            (self.vip, self.event, 'MKT-1', Decimal('100.00')),
            (self.reg, self.other_event, 'MKT-2', Decimal('50.00')),
            (self.no_market_vip, self.unassigned_event, 'MKT-3', Decimal('25.00')),
        ):
            TicketOrder.objects.create(
                customer=customer, event=event, uploaded_file=self.upload,
                order_number=order_number, order_date=timezone.now(), total_amount=total,
            )
        self.vip.last_order_date = date(2026, 1, 15)
        self.vip.save(update_fields=['last_order_date'])
        self.reg.last_order_date = date(2026, 2, 20)
        self.reg.save(update_fields=['last_order_date'])
        self.no_market_vip.last_order_date = date(2025, 12, 31)
        self.no_market_vip.save(update_fields=['last_order_date'])

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

    def test_market_filter(self):
        resp = self.client.get(reverse('tickets:customer_list'), {'market': str(self.market.id)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([c.email for c in resp.context['page_obj']], ['vip@x.com'])

    def test_filter_customers_market_id(self):
        from .services.customer_filters import filter_customers
        emails = set(
            filter_customers(self.org, {'market_id': str(self.market.id)})
            .distinct()
            .values_list('email', flat=True)
        )
        self.assertEqual(emails, {'vip@x.com'})

    def test_no_market_filter(self):
        from .services.customer_filters import NO_MARKET_VALUE
        resp = self.client.get(reverse('tickets:customer_list'), {'market': NO_MARKET_VALUE})
        self.assertEqual([c.email for c in resp.context['page_obj']], ['nomarket@x.com'])

    def test_filter_customers_no_market(self):
        from .services.customer_filters import NO_MARKET_VALUE, filter_customers
        emails = set(
            filter_customers(self.org, {'market_id': NO_MARKET_VALUE})
            .distinct()
            .values_list('email', flat=True)
        )
        self.assertEqual(emails, {'nomarket@x.com'})

    def test_filter_customers_market_ids(self):
        from .services.customer_filters import NO_MARKET_VALUE, filter_customers
        emails = set(
            filter_customers(self.org, {'market_ids': [str(self.market.id), NO_MARKET_VALUE]})
            .distinct()
            .values_list('email', flat=True)
        )
        self.assertEqual(emails, {'vip@x.com', 'nomarket@x.com'})

    def test_invalid_market_ignored(self):
        resp = self.client.get(reverse('tickets:customer_list'), {'market': 'not-a-uuid'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['market_filter'], '')
        emails = {c.email for c in resp.context['page_obj']}
        self.assertEqual(emails, {'vip@x.com', 'reg@x.com', 'nomarket@x.com'})

    def test_filter_customers_invalid_market_ignored(self):
        from .services.customer_filters import filter_customers
        emails = set(
            filter_customers(self.org, {'market_id': 'not-a-uuid'})
            .distinct()
            .values_list('email', flat=True)
        )
        self.assertEqual(emails, {'vip@x.com', 'reg@x.com', 'nomarket@x.com'})

    def test_market_segment_tag_filters_are_and_combined(self):
        resp = self.client.get(reverse('tickets:customer_list'), {
            'market': str(self.market.id),
            'segment': 'VIP',
            'tag': str(self.tag.id),
        })
        self.assertEqual([c.email for c in resp.context['page_obj']], ['vip@x.com'])

    def test_last_order_from_filter(self):
        resp = self.client.get(reverse('tickets:customer_list'), {'last_order_from': '2026-01-15'})
        emails = {c.email for c in resp.context['page_obj']}
        self.assertEqual(emails, {'vip@x.com', 'reg@x.com'})
        self.assertEqual(resp.context['last_order_from'], '2026-01-15')
        self.assertContains(resp, '2 matching customers')

    def test_last_order_to_filter(self):
        resp = self.client.get(reverse('tickets:customer_list'), {'last_order_to': '2026-01-15'})
        emails = {c.email for c in resp.context['page_obj']}
        self.assertEqual(emails, {'vip@x.com', 'nomarket@x.com'})
        self.assertEqual(resp.context['last_order_to'], '2026-01-15')

    def test_last_order_range_filter(self):
        resp = self.client.get(reverse('tickets:customer_list'), {
            'last_order_from': '2026-01-01',
            'last_order_to': '2026-01-31',
        })
        self.assertEqual([c.email for c in resp.context['page_obj']], ['vip@x.com'])

    def test_invalid_last_order_dates_ignored(self):
        resp = self.client.get(reverse('tickets:customer_list'), {
            'last_order_from': 'not-a-date',
            'last_order_to': 'also-bad',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['last_order_from'], '')
        self.assertEqual(resp.context['last_order_to'], '')
        self.assertFalse(resp.context['has_active_filters'])
        emails = {c.email for c in resp.context['page_obj']}
        self.assertEqual(emails, {'vip@x.com', 'reg@x.com', 'nomarket@x.com'})

    def test_customer_list_sort_links_preserve_market(self):
        resp = self.client.get(reverse('tickets:customer_list'), {'market': str(self.market.id)})
        self.assertContains(resp, f'market={self.market.id}')

    def test_customer_list_sort_links_preserve_last_order_filters(self):
        resp = self.client.get(reverse('tickets:customer_list'), {
            'last_order_from': '2026-01-01',
            'last_order_to': '2026-01-31',
        })
        self.assertContains(resp, 'last_order_from=2026-01-01')
        self.assertContains(resp, 'last_order_to=2026-01-31')

    def test_bulk_tag_select_all_respects_market_filter(self):
        resp = self.client.post(reverse('tickets:customers_bulk_tag'), {
            'select_all': '1',
            'market': str(self.other_market.id),
            'tag_mode': 'new',
            'new_tag_name': 'Seattle Buyers',
        })
        self.assertEqual(resp.status_code, 302)
        self.vip.refresh_from_db()
        self.reg.refresh_from_db()
        seattle = CustomerTag.objects.get(organization=self.org, name='Seattle Buyers')
        self.assertFalse(self.vip.tags.filter(id=seattle.id).exists())
        self.assertTrue(self.reg.tags.filter(id=seattle.id).exists())

    def test_bulk_tag_select_all_respects_last_order_filter(self):
        resp = self.client.post(reverse('tickets:customers_bulk_tag'), {
            'select_all': '1',
            'last_order_from': '2026-01-01',
            'last_order_to': '2026-01-31',
            'tag_mode': 'new',
            'new_tag_name': 'January Buyers',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn('last_order_from=2026-01-01', resp['Location'])
        self.assertIn('last_order_to=2026-01-31', resp['Location'])
        january = CustomerTag.objects.get(organization=self.org, name='January Buyers')
        self.assertTrue(self.vip.tags.filter(id=january.id).exists())
        self.assertFalse(self.reg.tags.filter(id=january.id).exists())
        self.assertFalse(self.no_market_vip.tags.filter(id=january.id).exists())

    # T9: bulk_sms_status with select_all respects market filter (mirrors bulk_tag)
    def test_bulk_sms_status_select_all_respects_market_filter(self):
        from .sms_views import customers_bulk_sms_status
        # Set both customers opted in initially
        self.vip.sms_opt_in = True
        self.vip.save(update_fields=['sms_opt_in'])
        self.reg.sms_opt_in = True
        self.reg.save(update_fields=['sms_opt_in'])
        # Opt out select_all in Austin market only
        resp = self.client.post(reverse('tickets:customers_bulk_sms_status'), {
            'select_all': '1',
            'market': str(self.market.id),
            'sms_opt_in': '0',
        })
        self.assertEqual(resp.status_code, 302)
        self.vip.refresh_from_db()
        self.reg.refresh_from_db()
        # Only Austin buyer (vip) should be opted out
        self.assertFalse(self.vip.sms_opt_in)
        self.assertTrue(self.reg.sms_opt_in)

    def test_customer_segments_market_scope_and_links(self):
        resp = self.client.get(reverse('tickets:customer_segments'), {'market': str(self.market.id)})
        self.assertEqual(resp.status_code, 200)
        by_segment = {row['segment']: row for row in resp.context['segment_stats']}
        self.assertEqual(resp.context['total_customers'], 1)
        self.assertEqual(by_segment['VIP']['count'], 1)
        self.assertEqual(by_segment['Loyal']['count'], 0)
        self.assertContains(resp, f'segment=VIP&market={self.market.id}')
        market_names = {row['market_name'] for row in resp.context['market_segment_breakdown']}
        self.assertIn('Austin', market_names)
        self.assertIn('No market', market_names)

    # T2: regression — order_count should not be inflated by tag join
    def test_order_count_not_inflated_by_tag_join(self):
        tag2 = CustomerTag.objects.create(organization=self.org, name='Press')
        self.vip.tags.add(tag2)  # vip now has 2 tags but still 1 order
        resp = self.client.get(reverse('tickets:customer_list'), {'segment': 'VIP'})
        self.assertEqual(resp.status_code, 200)
        customers = {c.email: c for c in resp.context['page_obj']}
        self.assertIn('vip@x.com', customers)
        self.assertEqual(customers['vip@x.com'].order_count, 1)

    # T16: customer with 2 orders in the same market must appear exactly once
    def test_list_dedup_customer_with_two_market_orders(self):
        TicketOrder.objects.create(
            customer=self.vip, event=self.event, uploaded_file=self.upload,
            order_number='MKT-DUP', order_date=timezone.now(), total_amount=Decimal('50.00'),
        )
        resp = self.client.get(reverse('tickets:customer_list'), {'market': str(self.market.id)})
        self.assertEqual(resp.status_code, 200)
        emails = [c.email for c in resp.context['page_obj']]
        self.assertEqual(emails.count('vip@x.com'), 1)

    # T13: segments page with __none__ market; breakdown pct and avg_ltv sanity
    def test_segments_no_market_scoped_stats(self):
        from .services.customer_filters import NO_MARKET_VALUE
        resp = self.client.get(reverse('tickets:customer_segments'), {'market': NO_MARKET_VALUE})
        self.assertEqual(resp.status_code, 200)
        by_segment = {row['segment']: row for row in resp.context['segment_stats']}
        # no_market_vip is Lapsed and the only customer in the no-market bucket
        self.assertEqual(resp.context['total_customers'], 1)
        self.assertEqual(by_segment['Lapsed']['count'], 1)
        self.assertEqual(by_segment['Lapsed']['pct'], 100.0)

    def test_segments_breakdown_pct_and_avg_ltv(self):
        resp = self.client.get(reverse('tickets:customer_segments'))
        self.assertEqual(resp.status_code, 200)
        breakdown = {row['market_id']: row for row in resp.context['market_segment_breakdown']}
        austin_row = breakdown.get(str(self.market.id), {})
        self.assertIn('segments', austin_row)
        seg_map = {s['segment']: s for s in austin_row['segments']}
        vip_seg = seg_map.get('VIP', {})
        # pct should be 100 since VIP is the only segment in Austin
        self.assertEqual(vip_seg.get('pct'), 100.0)
        # avg_ltv should be non-negative
        self.assertGreaterEqual(vip_seg.get('avg_ltv', -1), 0)

    # T14: form boundary — cross-org market UUID is silently ignored
    def test_cross_org_market_uuid_ignored_in_filter(self):
        other_org = Organization.objects.create(name='Other', slug='other-org', sms_marketing_enabled=True)
        foreign_market = Market.objects.create(
            organization=other_org, name='Foreign', geography_level='city', geography_value='X',
        )
        resp = self.client.get(reverse('tickets:customer_list'), {'market': str(foreign_market.id)})
        self.assertEqual(resp.status_code, 200)
        # Falls back to all-market view (market not in org's choices → filter = '')
        self.assertEqual(resp.context['market_filter'], '')

    # T14: __none__ market option gated on unassigned-market orders existing
    def test_no_market_option_hidden_when_all_events_have_markets(self):
        # Move the unassigned event to a market so no orders are market-less
        self.unassigned_event.market = self.other_market
        self.unassigned_event.save(update_fields=['market'])
        resp = self.client.get(reverse('tickets:customer_segments'))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context['has_no_market'])

    # T8: order-level AND — market_id + event_id must match the SAME order
    def test_order_level_and_semantics_market_plus_event(self):
        from .services.customer_filters import filter_customers
        # vip has an order for self.event (Austin market)
        # reg has an order for self.other_event (Seattle market)
        # Query: Austin market AND self.other_event → no customer has SAME order satisfying both
        result = set(
            filter_customers(self.org, {
                'market_id': str(self.market.id),
                'event_id': str(self.other_event.id),
            }).distinct().values_list('email', flat=True)
        )
        self.assertEqual(result, set())

    def test_order_level_and_semantics_matching_order(self):
        from .services.customer_filters import filter_customers
        # vip has an order for self.event which IS in Austin market
        result = set(
            filter_customers(self.org, {
                'market_id': str(self.market.id),
                'event_id': str(self.event.id),
            }).distinct().values_list('email', flat=True)
        )
        self.assertEqual(result, {'vip@x.com'})

    # T5: zero-market org sees no market UI and no breakdown
    def test_zero_market_org_hides_market_ui(self):
        # Create a fresh org with no markets
        org2 = Organization.objects.create(name='No Mkt', slug='no-mkt', sms_marketing_enabled=True)
        user2 = User.objects.create_user('u2', 'u2@test.com', 'pw')
        UserProfile.objects.create(user=user2, organization=org2, org_role=UserProfile.OrgRole.OWNER)
        client2 = Client()
        client2.login(username='u2@test.com', password='pw')
        client2.get(reverse('tickets:home'))
        resp = client2.get(reverse('tickets:customer_segments'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['market_choices'], [])
        self.assertEqual(resp.context['market_segment_breakdown'], [])


@override_settings(E2E_TEST_MODE=True, SMS_CAMPAIGN_MAX_RECIPIENTS=5000)
class SMSCampaignSendTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Org', slug='org-send', sms_marketing_enabled=True)
        for i in range(3):
            make_customer(self.org, f'c{i}@x.com', f'+1310555200{i}')

    def _campaign(self, **kwargs):
        return SMSCampaign.objects.create(
            organization=self.org, filter_criteria={'min_ltv': '0'}, name='C', body='Hi there',
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
        from .sms import with_stop_footer
        self.assertIn('Reply STOP to opt out', with_stop_footer('Hello'))
        self.assertEqual(with_stop_footer('Text STOP anytime'), 'Text STOP anytime')
        self.assertEqual(with_stop_footer('Reply STOP to cancel'), 'Reply STOP to cancel')

    def test_stop_footer_not_suppressed_by_casual_stop(self):
        # Only explicit opt-out phrasing suppresses the footer — a casual "stop"
        # must not strip the compliance disclosure.
        from .sms import with_stop_footer
        self.assertIn('Reply STOP to opt out', with_stop_footer('stop by the bar after the show'))
        self.assertIn('Reply STOP to opt out', with_stop_footer('Non-stop hits all night'))


@override_settings(E2E_TEST_MODE=True, SMS_FOOTER_DISCLOSURE_DAYS=30,
                   SMS_PRICE_PER_SEGMENT_CENTS=Decimal('3'))
class SMSConditionalFooterTests(TestCase):
    """Conditional STOP-footer disclosure: included on the first message to a phone
    and once every SMS_FOOTER_DISCLOSURE_DAYS, omitted in between. Decided + billed at
    schedule, honored at send. Covers apply_stop_footer, recently_disclosed_phones,
    plan_campaign_footers, and the send-task honor + safeguard paths."""

    def setUp(self):
        self.org = Organization.objects.create(name='Org', slug='org-footer', sms_marketing_enabled=True)

    def _disclosed(self, phone, *, days_ago=1, status=None, stop_disclosed=True, org=None):
        """Create a prior SENT recipient (a past disclosure) for a phone."""
        org = org or self.org
        c = SMSCampaign.objects.create(
            organization=org, filter_criteria={'min_ltv': '0'}, name='Past', body='Hi',
            status=SMSCampaign.Status.SENT,
        )
        return SMSMessageRecipient.objects.create(
            campaign=c, phone=phone,
            status=status or SMSMessageRecipient.Status.SENT,
            stop_disclosed=stop_disclosed,
            sent_at=timezone.now() - timedelta(days=days_ago),
        )

    def _scheduled_campaign(self, body='Hello'):
        return SMSCampaign.objects.create(
            organization=self.org, filter_criteria={'min_ltv': '0'}, name='C', body=body,
            status=SMSCampaign.Status.SCHEDULED, scheduled_at=timezone.now() - timedelta(minutes=1),
        )

    def _run_send(self, campaign):
        sent = {}

        def fake(to, body, status_callback=None):
            sent[to] = body
            return True, 'SM' + to[-4:]

        from .tasks import send_sms_campaign_task
        with patch('tickets.sms.send_sms', side_effect=fake):
            send_sms_campaign_task.delay(str(campaign.id))
        return sent

    # --- apply_stop_footer (pure) ---
    def test_apply_stop_footer_branches(self):
        from .sms import apply_stop_footer
        self.assertEqual(apply_stop_footer('Hello', include=True), ('Hello\n\nReply STOP to opt out', True))
        self.assertEqual(apply_stop_footer('Hello', include=False), ('Hello', False))
        # Explicit opt-out copy already in body: never appended, always a disclosure —
        # including when include=False (the key branch that makes stop_disclosed honest).
        self.assertEqual(apply_stop_footer('Reply STOP to cancel', include=True), ('Reply STOP to cancel', True))
        self.assertEqual(apply_stop_footer('Reply STOP to cancel', include=False), ('Reply STOP to cancel', True))
        self.assertEqual(apply_stop_footer('', include=False), ('', False))

    # --- recently_disclosed_phones ---
    def test_recently_disclosed_includes_sent_and_delivered(self):
        self._disclosed('+13105550001', status=SMSMessageRecipient.Status.SENT)
        self._disclosed('+13105550002', status=SMSMessageRecipient.Status.DELIVERED)
        got = SMSMessageRecipient.recently_disclosed_phones(
            self.org, ['+13105550001', '+13105550002'], timezone.now())
        self.assertEqual(got, {'+13105550001', '+13105550002'})

    def test_recently_disclosed_excludes_undelivered_failed_queued(self):
        self._disclosed('+13105550003', status=SMSMessageRecipient.Status.UNDELIVERED)
        self._disclosed('+13105550004', status=SMSMessageRecipient.Status.FAILED)
        self._disclosed('+13105550005', status=SMSMessageRecipient.Status.QUEUED)
        got = SMSMessageRecipient.recently_disclosed_phones(
            self.org, ['+13105550003', '+13105550004', '+13105550005'], timezone.now())
        self.assertEqual(got, set())

    def test_recently_disclosed_excludes_non_disclosed_rows(self):
        self._disclosed('+13105550006', stop_disclosed=False)
        got = SMSMessageRecipient.recently_disclosed_phones(self.org, ['+13105550006'], timezone.now())
        self.assertEqual(got, set())

    def test_recently_disclosed_excludes_outside_window(self):
        self._disclosed('+13105550007', days_ago=31)
        got = SMSMessageRecipient.recently_disclosed_phones(self.org, ['+13105550007'], timezone.now())
        self.assertEqual(got, set())

    def test_recently_disclosed_is_org_scoped(self):
        # Multi-tenancy: another org's disclosure to the same number must not count.
        other = Organization.objects.create(name='Other', slug='org-other', sms_marketing_enabled=True)
        self._disclosed('+13105550008', org=other)
        got = SMSMessageRecipient.recently_disclosed_phones(self.org, ['+13105550008'], timezone.now())
        self.assertEqual(got, set())

    def test_recently_disclosed_empty_phones(self):
        self.assertEqual(
            SMSMessageRecipient.recently_disclosed_phones(self.org, [], timezone.now()), set())

    # --- plan_campaign_footers ---
    def test_plan_first_ever_includes_footer(self):
        from .services.sms_credits import plan_campaign_footers
        cents, plan = plan_campaign_footers(self.org, 'Hello', ['+13105550010'], as_of=timezone.now())
        present, seg = plan['+13105550010']
        self.assertTrue(present)
        self.assertEqual(seg, 1)
        self.assertEqual(cents, 3)

    def test_plan_recently_disclosed_omits_footer(self):
        from .services.sms_credits import plan_campaign_footers
        self._disclosed('+13105550011', days_ago=5)
        cents, plan = plan_campaign_footers(self.org, 'Hello', ['+13105550011'], as_of=timezone.now())
        self.assertFalse(plan['+13105550011'][0])

    def test_plan_aged_out_reincludes_footer(self):
        from .services.sms_credits import plan_campaign_footers
        self._disclosed('+13105550012', days_ago=31)
        cents, plan = plan_campaign_footers(self.org, 'Hello', ['+13105550012'], as_of=timezone.now())
        self.assertTrue(plan['+13105550012'][0])

    def test_plan_cost_is_sum_of_segments(self):
        from .services.sms_credits import plan_campaign_footers
        # Two first-ever phones, 1 segment each -> 2 x 3c = 6c.
        cents, _ = plan_campaign_footers(
            self.org, 'Hello', ['+13105550013', '+13105550014'], as_of=timezone.now())
        self.assertEqual(cents, 6)

    def test_plan_duplicate_phone_charged_per_send(self):
        from .services.sms_credits import plan_campaign_footers
        # Dict dedups, but cost counts both sends: 2 segments x 3c = 6c.
        cents, plan = plan_campaign_footers(
            self.org, 'Hello', ['+13105550015', '+13105550015'], as_of=timezone.now())
        self.assertEqual(cents, 6)
        self.assertEqual(len(plan), 1)

    def test_plan_empty_phones(self):
        from .services.sms_credits import plan_campaign_footers
        self.assertEqual(
            plan_campaign_footers(self.org, 'Hello', [], as_of=timezone.now()), (0, {}))

    # --- send task: honor persisted decision + safeguard ---
    def test_send_honors_omit_when_recently_disclosed(self):
        self._disclosed('+13105559200', days_ago=5)  # real recent disclosure
        c = self._scheduled_campaign()
        SMSMessageRecipient.objects.create(campaign=c, phone='+13105559200', stop_disclosed=False, segments=1)
        sent = self._run_send(c)
        self.assertNotIn('Reply STOP', sent['+13105559200'])

    def test_send_includes_when_disclosed_true(self):
        c = self._scheduled_campaign()
        SMSMessageRecipient.objects.create(campaign=c, phone='+13105559201', stop_disclosed=True, segments=1)
        sent = self._run_send(c)
        self.assertIn('Reply STOP to opt out', sent['+13105559201'])

    def test_send_safeguard_reincludes_when_disclosure_aged_out(self):
        # Planned omit (stop_disclosed=False) but the prior disclosure aged out of the
        # window before this delayed send -> safeguard re-adds the footer (compliance).
        self._disclosed('+13105559202', days_ago=31)
        c = self._scheduled_campaign()
        SMSMessageRecipient.objects.create(campaign=c, phone='+13105559202', stop_disclosed=False, segments=1)
        sent = self._run_send(c)
        self.assertIn('Reply STOP to opt out', sent['+13105559202'])


@override_settings(E2E_TEST_MODE=True, SMS_CAMPAIGN_MAX_RECIPIENTS=5000,
                   SMS_PRICE_PER_SEGMENT_CENTS=Decimal('3'), SMS_FOOTER_DISCLOSURE_DAYS=30)
class SMSConditionalFooterFlowTests(TestCase):
    """End-to-end through the compose/confirm view: the footer decision is billed at
    schedule, displayed == charged, and a repeat send to the same phones omits it."""

    def setUp(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Org', slug='org-cf', sms_marketing_enabled=True)
        self.user = User.objects.create_user('u', 'u@test.com', 'pw')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='u@test.com', password='pw')
        self.client.get(reverse('tickets:home'))
        for i in range(2):
            make_customer(self.org, f'c{i}@x.com', f'+1310555910{i}')
        self.org.sms_credit_balance_cents = 1000
        self.org.save(update_fields=['sms_credit_balance_cents'])

    def _send(self, body):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse('tickets:sms_campaign_create'), {
                'name': 'Promo', 'rfm_segment': 'VIP', 'body': body,
                'send_mode': 'now', 'confirm': '1',
            })

    def test_preview_matches_charge(self):
        # 150-char body: 1 segment without the footer, 2 with (footer is 23 chars).
        body = 'x' * 150
        resp = self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': 'Promo', 'rfm_segment': 'VIP', 'body': body, 'send_mode': 'now',
        })
        self.assertEqual(resp.context['confirm_cost_cents'], 12)   # 2 recip x 2 seg x 3c
        self.assertEqual(resp.context['confirm_cost_tokens'], 4)   # 2 recip x 2 seg

    def test_review_token_cost_reflects_mixed_audience(self):
        # One phone was disclosed recently (footer omitted -> 1 seg), the other is
        # first-ever (footer -> 2 seg). The review panel's token cost must show the
        # mix (3), not the worst-case-for-all (4).
        past = SMSCampaign.objects.create(
            organization=self.org, filter_criteria={'min_ltv': '0'}, name='Past', body='Hi',
            status=SMSCampaign.Status.SENT,
        )
        SMSMessageRecipient.objects.create(
            campaign=past, phone='+13105559100',
            status=SMSMessageRecipient.Status.SENT, stop_disclosed=True,
            sent_at=timezone.now() - timedelta(days=5),
        )
        body = 'x' * 150
        resp = self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': 'Promo', 'rfm_segment': 'VIP', 'body': body, 'send_mode': 'now',
        })
        self.assertEqual(resp.context['confirm_cost_tokens'], 3)   # 1 (disclosed) + 2 (first-ever)
        self.assertEqual(resp.context['confirm_cost_cents'], 9)    # 3 seg x 3c

    def test_first_send_discloses_second_omits_and_charges_less(self):
        body = 'x' * 150  # footer pushes 1 segment -> 2
        self._send(body)
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 988)   # 1000 - 2x2x3
        first = SMSCampaign.objects.latest('created_at')
        self.assertTrue(all(r.stop_disclosed and r.segments == 2 for r in first.recipients.all()))

        self._send(body)  # same phones, within 30d -> footer omitted
        self.org.refresh_from_db()
        self.assertEqual(self.org.sms_credit_balance_cents, 982)   # - 2x1x3
        second = SMSCampaign.objects.exclude(id=first.id).latest('created_at')
        self.assertTrue(all((not r.stop_disclosed) and r.segments == 1 for r in second.recipients.all()))


@override_settings(E2E_TEST_MODE=True)
class SMSSchedulerCommandTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Org', slug='org-sch', sms_marketing_enabled=True)
        make_customer(self.org, 'a@x.com', '+13105553001')

    def _campaign(self, status, scheduled_at=None, started_at=None):
        return SMSCampaign.objects.create(
            organization=self.org, filter_criteria={'min_ltv': '0'}, name='C', body='Hi',
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
        self.campaign = SMSCampaign.objects.create(
            organization=self.org, filter_criteria={'min_ltv': '0'}, name='C', body='Hi',
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
        self.customer = make_customer(self.org, 'a@x.com', '+13105555001')
        self.other_customer = make_customer(self.org, 'b@x.com', '+13105555002')
        self.venue = Venue.objects.create(organization=self.org, name='Hall', city='Austin')
        self.market = Market.objects.create(
            organization=self.org, name='Austin', geography_level='city', geography_value='Austin',
        )
        self.event = Event.objects.create(
            organization=self.org, name='Market Show', venue=self.venue,
            market=self.market, start_date=date(2026, 1, 1),
        )
        self.fmt = CSVFormat.objects.create(organization=self.org, name='F', column_mapping={'order_number': 'O'})
        self.upload = UploadedFile.objects.create(
            organization=self.org, csv_format=self.fmt, filename='f.csv', status='completed',
        )
        TicketOrder.objects.create(
            customer=self.customer, event=self.event, uploaded_file=self.upload,
            order_number='SMS-MKT-1', order_date=timezone.now(), total_amount=Decimal('40.00'),
        )

    def test_native_compose_gated_when_disabled(self):
        # Native send/compose views stay gated behind sms_marketing_enabled,
        # even though the SMS list page itself is always reachable for hosts.
        self.org.sms_marketing_enabled = False
        self.org.save(update_fields=['sms_marketing_enabled'])
        resp = self.client.get(reverse('tickets:sms_campaign_create'))
        self.assertEqual(resp.status_code, 404)

    def test_campaign_list_ok_when_enabled(self):
        resp = self.client.get(reverse('tickets:sms_campaign_list'))
        self.assertEqual(resp.status_code, 200)
        # Consolidated SMS home: shared Marketing nav + performance band + table.
        self.assertContains(resp, 'marketing-sectionnav')
        self.assertContains(resp, 'Campaigns sent')   # native performance stat card
        self.assertContains(resp, 'Your campaigns')    # campaign table section

    def test_linked_sms_visible_when_native_disabled_but_slicktext_linked(self):
        # Native SMS off, but SlickText connected → the SMS tab/page stays
        # reachable in linked-only mode (SlickText results, no native UI).
        from datetime import date, time
        from .models import Venue, Event, EventSMSCampaign
        self.org.sms_marketing_enabled = False
        self.org.slicktext_api_key = 'st-key'
        self.org.slicktext_brand_id = 'brand-1'
        self.org.save(update_fields=[
            'sms_marketing_enabled', 'slicktext_api_key', 'slicktext_brand_id',
        ])
        venue = Venue.objects.create(organization=self.org, name='Hall', city='LA')
        event = Event.objects.create(
            organization=self.org, name='Show', venue=venue,
            start_date=date(2026, 6, 1), start_time=time(20, 0, 0),
        )
        EventSMSCampaign.objects.create(
            event=event, source='slicktext', external_id='st-9', name='Blast',
            send_time=timezone.now() - timedelta(days=2),
            audience_size=500, unique_clicks=40, orders=3, revenue=Decimal('150.00'),
            confirmed_at=timezone.now(),
        )

        resp = self.client.get(reverse('tickets:sms_campaign_list'))
        self.assertEqual(resp.status_code, 200)
        # Linked sections present...
        self.assertContains(resp, 'SlickText (linked)')
        self.assertContains(resp, 'Top SlickText broadcasts')
        # ...native compose/send UI absent.
        self.assertNotContains(resp, 'New Campaign')
        self.assertNotContains(resp, 'Campaigns sent')
        self.assertNotContains(resp, 'Your campaigns')

    def test_sms_page_reachable_when_native_disabled_and_no_slicktext(self):
        # SMS tab is always available to hosts: the list page loads (200) even
        # with native off and no SlickText, just without any native UI.
        self.org.sms_marketing_enabled = False
        self.org.save(update_fields=['sms_marketing_enabled'])
        resp = self.client.get(reverse('tickets:sms_campaign_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'New Campaign')
        self.assertNotContains(resp, 'Your campaigns')

    def test_marketing_overview_has_unified_section_nav(self):
        # All channels live in the Marketing page's primary section nav; the old
        # in-page Bootstrap tab row (Overview/Email/Paid Ads) is gone.
        resp = self.client.get(reverse('tickets:marketing_overview'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'marketing-sectionnav')
        self.assertContains(resp, reverse('tickets:sms_campaign_list'))
        # Email + Paid Ads are now top-level section links, not in-page tabs.
        self.assertContains(resp, reverse('tickets:marketing_overview') + '?tab=email')
        self.assertContains(resp, reverse('tickets:marketing_overview') + '?tab=ads')
        self.assertNotContains(resp, 'marketing-tabs')
        self.assertNotContains(resp, 'data-tab-key')

    def test_marketing_overview_renders_each_section(self):
        # Each section is now server-rendered via ?tab= (no in-page tab JS);
        # only the active section's body markup is present.
        email = self.client.get(reverse('tickets:marketing_overview'), {'tab': 'email'})
        self.assertEqual(email.status_code, 200)
        self.assertContains(email, 'Top email campaigns')
        self.assertNotContains(email, 'Top events by ROI')   # ads-only section
        ads = self.client.get(reverse('tickets:marketing_overview'), {'tab': 'ads'})
        self.assertEqual(ads.status_code, 200)
        self.assertContains(ads, 'Top events by ROI')
        self.assertNotContains(ads, 'Top email campaigns')   # email-only section

    def test_preview_returns_count(self):
        resp = self.client.post(reverse('tickets:sms_audience_preview'), {'rfm_segment': 'VIP'})
        self.assertEqual(resp.json()['count'], 2)

    def test_preview_filters_segment_by_market(self):
        resp = self.client.post(reverse('tickets:sms_audience_preview'), {
            'rfm_segment': 'VIP',
            'market_id': str(self.market.id),
        })
        self.assertEqual(resp.json()['count'], 1)

    def test_compose_get_renders_audience_picker(self):
        resp = self.client.get(reverse('tickets:sms_campaign_create'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="audience-section"')
        self.assertContains(resp, 'name="rfm_segment"')
        self.assertContains(resp, 'name="market_id"')

    def test_empty_audience_rejected(self):
        # No tag/segment and no event → form invalid, no campaign created.
        resp = self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': 'Promo', 'body': 'Hello', 'send_mode': 'now',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(SMSCampaign.objects.count(), 0)

    # T3: market alone cannot be the sole audience selector
    def test_market_only_audience_rejected(self):
        resp = self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': 'Promo', 'body': 'Hello', 'send_mode': 'now',
            'market_id': str(self.market.id),
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(SMSCampaign.objects.count(), 0)
        errors = [str(e) for e in resp.context['form'].non_field_errors()]
        self.assertIn('Choose at least one tag or segment.', errors)

    def test_create_requires_confirm_before_send(self):
        # First POST without confirm: shows count, does NOT create.
        resp = self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': 'Promo', 'rfm_segment': 'VIP', 'body': 'Hello',
            'send_mode': 'now',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['confirm_count'], 2)
        self.assertEqual(SMSCampaign.objects.count(), 0)

    def test_create_with_market_saves_market_criteria(self):
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(reverse('tickets:sms_campaign_create'), {
                'name': 'Promo', 'rfm_segment': 'VIP', 'market_id': str(self.market.id),
                'body': 'Hello', 'send_mode': 'now', 'confirm': '1',
            })
        self.assertEqual(resp.status_code, 302)
        campaign = SMSCampaign.objects.get()
        self.assertEqual(campaign.filter_criteria, {
            'rfm_segment': ['VIP'],
            'market_id': str(self.market.id),
        })
        self.assertIn('Markets: Austin', campaign.audience_summary(self.org))

    def test_create_with_confirm_sends(self):
        # Send-now dispatches via transaction.on_commit — capture so it fires in TestCase.
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(reverse('tickets:sms_campaign_create'), {
                'name': 'Promo', 'rfm_segment': 'VIP', 'body': 'Hello',
                'send_mode': 'now', 'confirm': '1',
            })
        self.assertEqual(resp.status_code, 302)
        c = SMSCampaign.objects.get()
        self.assertEqual(c.status, SMSCampaign.Status.SENT)

    def test_schedule_creates_scheduled_campaign(self):
        when = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        resp = self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': 'Later', 'rfm_segment': 'VIP', 'body': 'Hello',
            'send_mode': 'schedule', 'scheduled_at': when, 'confirm': '1',
        })
        self.assertEqual(resp.status_code, 302)
        c = SMSCampaign.objects.get()
        self.assertEqual(c.status, SMSCampaign.Status.SCHEDULED)
        self.assertIsNotNone(c.scheduled_at)

    def test_cancel_scheduled(self):
        c = SMSCampaign.objects.create(
            organization=self.org, filter_criteria={'min_ltv': '0'}, name='S', body='Hi',
            status=SMSCampaign.Status.SCHEDULED, scheduled_at=timezone.now() + timedelta(days=1),
        )
        resp = self.client.post(reverse('tickets:sms_campaign_cancel', kwargs={'pk': c.id}))
        self.assertEqual(resp.status_code, 302)
        c.refresh_from_db()
        self.assertEqual(c.status, SMSCampaign.Status.CANCELED)

    def test_cross_tenant_detail_404(self):
        other = Organization.objects.create(name='Org B', slug='org-vb', sms_marketing_enabled=True)
        oc = SMSCampaign.objects.create(
            organization=other, filter_criteria={'min_ltv': '0'}, name='Theirs', body='Hi',
            status=SMSCampaign.Status.DRAFT,
        )
        resp = self.client.get(reverse('tickets:sms_campaign_detail', kwargs={'pk': oc.id}))
        self.assertEqual(resp.status_code, 404)


@override_settings(E2E_TEST_MODE=True)
class SMSAnalyticsTests(TestCase):
    def test_native_summary_counts(self):
        from .services.marketing.analytics import MarketingAnalyticsService
        org = Organization.objects.create(name='Org', slug='org-an', sms_marketing_enabled=True)
        c = SMSCampaign.objects.create(
            organization=org, filter_criteria={'min_ltv': '0'}, name='C', body='Hi',
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

    def _campaign(self, body, link_url):
        return SMSCampaign.objects.create(
            organization=self.org, filter_criteria={'min_ltv': '0'}, name='C', body=body,
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
        self.campaign = SMSCampaign.objects.create(
            organization=self.org, filter_criteria={'min_ltv': '0'}, name='C', body='Hi https://shop.co/x',
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

    def _recipient(self, sent_offset_min, campaign=None):
        c = campaign or SMSCampaign.objects.create(
            organization=self.org, filter_criteria={'min_ltv': '0'}, name='C', body='Hi',
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
        c = SMSCampaign.objects.create(
            organization=org, filter_criteria={'min_ltv': '0'}, name='C', body='Hi https://s.co/x',
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

    def test_create_derives_link_url_from_body(self):
        self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': 'Promo', 'rfm_segment': 'VIP',
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

    def test_event_audience_resolves_opted_in_only(self):
        # An event-mode campaign targets the event's attendees via event_id.
        c = SMSCampaign(
            organization=self.org, name='C', body='Hi',
            filter_criteria={'event_id': str(self.event.id)},
        )
        phones = {r['phone'] for r in c.materialize(self.org)}
        self.assertEqual(phones, {'+13105550001', '+13105550002'})  # opt-out + non-attendee excluded

    def test_create_event_mode_get_seeds_form(self):
        resp = self.client.get(reverse('tickets:sms_campaign_create'), {'event': str(self.event.id)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['event'], self.event)
        form = resp.context['form']
        self.assertEqual(form.initial.get('name'), self.event.name)

    def test_create_event_mode_sends_to_attendees_and_links_event(self):
        resp = self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': self.event.name, 'body': 'See you there!',
            'send_mode': 'now', 'event': str(self.event.id), 'confirm': '1',
        })
        self.assertEqual(resp.status_code, 302)
        c = SMSCampaign.objects.get()
        self.assertEqual(c.event_id, self.event.id)
        self.assertEqual(c.filter_criteria, {'event_id': str(self.event.id)})
        phones = set(SMSMessageRecipient.objects.filter(campaign=c).values_list('phone', flat=True))
        self.assertEqual(phones, {'+13105550001', '+13105550002'})

    def test_create_event_mode_get_renders_audience_scope_choice(self):
        resp = self.client.get(reverse('tickets:sms_campaign_create'), {'event': str(self.event.id)})
        self.assertContains(resp, 'name="audience_scope"')
        self.assertContains(resp, 'All SMS subscribers')
        self.assertContains(resp, 'Ticket buyers for this event')

    def test_audience_preview_all_subscribers_widens_beyond_buyers(self):
        # Event scope (default) → only the two opted-in buyers.
        buyers = self.client.post(reverse('tickets:sms_audience_preview'), {
            'event': str(self.event.id),
        })
        self.assertEqual(buyers.json()['count'], 2)
        # All-subscribers scope → also the opted-in non-attendee (opt-out still excluded).
        everyone = self.client.post(reverse('tickets:sms_audience_preview'), {
            'event': str(self.event.id), 'audience_scope': 'all',
        })
        self.assertEqual(everyone.json()['count'], 3)

    def test_create_event_mode_all_subscribers_sends_org_wide_and_links_event(self):
        resp = self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': self.event.name, 'body': 'Big news for everyone!',
            'send_mode': 'now', 'event': str(self.event.id),
            'audience_scope': 'all', 'confirm': '1',
        })
        self.assertEqual(resp.status_code, 302)
        c = SMSCampaign.objects.get()
        self.assertEqual(c.event_id, self.event.id)  # still linked to the event
        self.assertEqual(c.filter_criteria, {'all_subscribers': True})  # no event_id narrowing
        phones = set(SMSMessageRecipient.objects.filter(campaign=c).values_list('phone', flat=True))
        # a1 + a2 (buyers) + non-attendee; opt-out excluded.
        self.assertEqual(phones, {'+13105550001', '+13105550002', '+13105550004'})

    def test_review_preserves_all_subscribers_scope(self):
        # Regression: the review POST re-renders the form; the chosen scope chip must
        # stay checked or the confirm POST silently falls back to ticket buyers.
        resp = self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': self.event.name, 'body': 'Big news for everyone!',
            'send_mode': 'now', 'event': str(self.event.id), 'audience_scope': 'all',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['confirm_count'], 3)  # org-wide, not the 2 buyers
        self.assertContains(resp, 'value="all" checked')
        self.assertNotContains(resp, 'value="event" checked')
        # The review button stays in the DOM (hidden) so the JS can swap it back in
        # when an audience/body edit invalidates the stale confirm panel.
        self.assertContains(resp, 'id="review-btn" hidden')

    def test_review_preserves_tag_scope(self):
        tag = CustomerTag.objects.create(organization=self.org, name='Press')
        self.a1.tags.add(tag)
        resp = self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': self.event.name, 'body': 'Press release!',
            'send_mode': 'now', 'event': str(self.event.id),
            'audience_scope': 'tag', 'tag_ids': [str(tag.id)],
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['confirm_count'], 1)
        self.assertContains(resp, 'value="tag" checked')
        self.assertNotContains(resp, 'value="event" checked')

    def test_create_event_mode_renders_tag_scope_when_org_has_tags(self):
        CustomerTag.objects.create(organization=self.org, name='Press')
        resp = self.client.get(reverse('tickets:sms_campaign_create'), {'event': str(self.event.id)})
        self.assertContains(resp, 'Customers with a tag')

    def test_audience_preview_tag_scope_targets_tagged_opted_in_org_wide(self):
        tag = CustomerTag.objects.create(organization=self.org, name='Press')
        self.a1.tags.add(tag)       # a buyer
        self.nonatt.tags.add(tag)   # a non-buyer — proves it's not event-scoped
        self.optout.tags.add(tag)   # opted-out — must be excluded
        resp = self.client.post(reverse('tickets:sms_audience_preview'), {
            'event': str(self.event.id), 'audience_scope': 'tag', 'tag_ids': [str(tag.id)],
        })
        self.assertEqual(resp.json()['count'], 2)  # a1 + nonatt

    def test_create_event_mode_tag_scope_sends_to_tagged_and_links_event(self):
        tag = CustomerTag.objects.create(organization=self.org, name='Press')
        self.a1.tags.add(tag)
        self.nonatt.tags.add(tag)
        resp = self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': self.event.name, 'body': 'Press release!',
            'send_mode': 'now', 'event': str(self.event.id),
            'audience_scope': 'tag', 'tag_ids': [str(tag.id)], 'confirm': '1',
        })
        self.assertEqual(resp.status_code, 302)
        c = SMSCampaign.objects.get()
        self.assertEqual(c.event_id, self.event.id)  # still linked to the event
        self.assertEqual(c.filter_criteria, {'tag_ids': [str(tag.id)]})  # no event_id narrowing
        phones = set(SMSMessageRecipient.objects.filter(campaign=c).values_list('phone', flat=True))
        self.assertEqual(phones, {'+13105550001', '+13105550004'})  # a1 (buyer) + nonatt

    def test_create_event_mode_tag_scope_without_tag_is_rejected(self):
        resp = self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': self.event.name, 'body': 'Oops no tag',
            'send_mode': 'now', 'event': str(self.event.id), 'audience_scope': 'tag',
        })
        self.assertEqual(resp.status_code, 200)  # re-renders with an error, no send
        self.assertContains(resp, 'Pick at least one tag to send to.')
        self.assertEqual(SMSCampaign.objects.count(), 0)

    def test_cross_tenant_event_404(self):
        from datetime import date
        from .models import Event, Venue
        other = Organization.objects.create(name='B', slug='org-evb', sms_marketing_enabled=True)
        ov = Venue.objects.create(organization=other, name='V', city='SF')
        oe = Event.objects.create(organization=other, name='Theirs', venue=ov, start_date=date(2026, 7, 1))
        resp = self.client.get(reverse('tickets:sms_campaign_create'), {'event': str(oe.id)})
        self.assertEqual(resp.status_code, 404)

    # T15: stray market_id POST in event-mode must be discarded — criteria saves
    # only event_id, not any market_id that might appear in hidden form fields.
    def test_event_mode_stray_market_id_is_discarded(self):
        from .models import Market
        mkt = Market.objects.create(
            organization=self.org, name='LA', geography_level='city', geography_value='LA',
        )
        resp = self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': 'Fest', 'body': 'See you!',
            'send_mode': 'now', 'event': str(self.event.id),
            'market_id': str(mkt.id),  # stray hidden field from non-event mode
            'confirm': '1',
        })
        self.assertEqual(resp.status_code, 302)
        c = SMSCampaign.objects.get()
        # market_id must NOT appear in persisted criteria
        self.assertNotIn('market_id', c.filter_criteria)
        self.assertEqual(c.filter_criteria, {'event_id': str(self.event.id)})

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
        c = SMSCampaign.objects.create(organization=self.org, filter_criteria={'min_ltv': '0'}, name='C', body='Hi',
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

    def _post_send(self, **extra):
        data = {
            'name': 'Promo', 'rfm_segment': 'VIP', 'body': 'Hello',
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
            'name': 'Promo', 'rfm_segment': 'VIP', 'body': 'Hello', 'send_mode': 'now',
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
            'name': 'Later', 'rfm_segment': 'VIP', 'body': 'Hello',
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

    def _confirm(self, key, **extra):
        data = {'name': 'Promo', 'rfm_segment': 'VIP', 'body': 'Hello',
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
            'name': 'P', 'rfm_segment': 'VIP', 'body': 'Hi',
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
            'name': 'P', 'rfm_segment': 'VIP', 'body': 'Hi',
            'send_mode': 'now', 'confirm': '1', 'idempotency_key': 'frz-1',
        })
        c = SMSCampaign.objects.get()
        self.assertEqual(c.audience_size, 2)
        self.assertEqual(SMSMessageRecipient.objects.filter(campaign=c).count(), 2)

    def test_opt_out_after_freeze_is_skipped_at_send(self):
        # Freeze the audience, then a recipient opts out before the send fires.
        self.client.post(reverse('tickets:sms_campaign_create'), {
            'name': 'P', 'rfm_segment': 'VIP', 'body': 'Hi',
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

    def test_cost_tokens_includes_stop_footer_segments(self):
        # The auto-appended footer ("\n\nReply STOP to opt out", 23 GSM-7 chars) is
        # part of what's sent and billed: 150 typed chars look like 1 segment but
        # send as 173 chars = 2 segments. The composer meter mirrors this math.
        from .services.sms_credits import estimate_campaign_cost_tokens
        self.assertEqual(estimate_campaign_cost_tokens(572, 'x' * 150), 1144)
        # Explicit opt-out phrasing → no footer added → stays 1 segment.
        self.assertEqual(estimate_campaign_cost_tokens(572, 'x' * 130 + ' Reply STOP to end'), 572)


@override_settings(E2E_TEST_MODE=True, SITE_URL='https://test.cueup.co')
class SMSTicketLinkRevenueTests(TestCase):
    """Detail-page attribution: tickets bought + NET revenue for the tracked ticket
    link (/track/<token>/) the composer inserts into a campaign body. Resolution is
    by the token in the body, so no SMSCampaign model change is needed."""

    def setUp(self):
        from datetime import timedelta as _td
        from .models import Event, Venue, TICKETING_TYPE_DIRECT, EVENT_STATUS_LIVE
        self.client = Client()
        self.org = Organization.objects.create(
            name='Org', slug='org-tl', sms_marketing_enabled=True,
            sms_credit_balance_cents=100000,
        )
        self.user = User.objects.create_user('u', 'u@test.com', 'pw')
        UserProfile.objects.create(user=self.user, organization=self.org,
                                   org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='u@test.com', password='pw')
        self.client.get(reverse('tickets:home'))
        venue = Venue.objects.create(organization=self.org, name='V', city='LA')
        self.event = Event.objects.create(
            organization=self.org, name='Live Fest', venue=venue,
            start_date=timezone.now().date() + _td(days=30),
            ticketing_type=TICKETING_TYPE_DIRECT, status=EVENT_STATUS_LIVE,
        )

    def _link(self):
        from .models import TrackingLink, _generate_tracking_token
        return TrackingLink.objects.create(
            organization=self.org, event=self.event, name='SMS',
            token=_generate_tracking_token(),
        )

    def _campaign(self, link_url):
        return SMSCampaign.objects.create(
            organization=self.org, name='C', body='hi', link_url=link_url,
            status=SMSCampaign.Status.SENT,
        )

    def _completed_session(self, tracking_link, n_tickets, gross_cents, fee_cents, status=None):
        from .models import TicketOrder, Ticket, StripeCheckoutSession
        n = StripeCheckoutSession.objects.count()
        cust = Customer.objects.create(organization=self.org, email=f'b{n}@x.com', name='B')
        order = TicketOrder.objects.create(
            customer=cust, event=self.event, order_number=f'O-{n}',
            order_date=timezone.now(), total_amount=Decimal(gross_cents) / 100,
        )
        for _ in range(n_tickets):
            Ticket.objects.create(ticket_order=order, ticket_type='GA', price=Decimal('10.00'))
        return StripeCheckoutSession.objects.create(
            event=self.event, organization=self.org, stripe_session_id=f'cs_{n}',
            buyer_email=cust.email, status=status or StripeCheckoutSession.Status.COMPLETED,
            amount_total_cents=gross_cents, platform_fee_cents=fee_cents,
            ticket_order=order, tracking_link=tracking_link,
        )

    def _stats(self, campaign):
        resp = self.client.get(reverse('tickets:sms_campaign_detail', kwargs={'pk': campaign.id}))
        self.assertEqual(resp.status_code, 200)
        return resp.context['buy_stats']

    def test_detail_shows_tickets_and_net_revenue(self):
        tl = self._link()
        c = self._campaign(f'https://test.cueup.co/track/{tl.token}/')
        self._completed_session(tl, 2, 5000, 500)
        self._completed_session(tl, 1, 3000, 300)
        stats = self._stats(c)
        self.assertEqual(stats['tickets'], 3)
        self.assertEqual(stats['orders'], 2)
        self.assertEqual(stats['revenue'], Decimal('72.00'))   # (5000-500)+(3000-300) = 7200c

    def test_detail_excludes_non_completed_sessions(self):
        from .models import StripeCheckoutSession
        tl = self._link()
        c = self._campaign(f'https://test.cueup.co/track/{tl.token}/')
        self._completed_session(tl, 2, 5000, 500)
        self._completed_session(tl, 5, 9999, 0, status=StripeCheckoutSession.Status.PENDING)
        self._completed_session(tl, 5, 9999, 0, status=StripeCheckoutSession.Status.REFUNDED)
        self._completed_session(tl, 5, 9999, 0, status=StripeCheckoutSession.Status.PARTIALLY_REFUNDED)
        stats = self._stats(c)
        self.assertEqual(stats['tickets'], 2)
        self.assertEqual(stats['orders'], 1)
        self.assertEqual(stats['revenue'], Decimal('45.00'))

    def test_detail_no_tracked_link_has_no_buy_stats(self):
        # Plain campaign (no /track/ link in the body) -> no attribution cards.
        self.assertIsNone(self._stats(self._campaign('https://example.com/x')))
        self.assertIsNone(self._stats(self._campaign('')))

    def test_detail_unknown_token_has_no_buy_stats(self):
        # Body links a token with no matching TrackingLink -> graceful None.
        self.assertIsNone(self._stats(self._campaign('https://test.cueup.co/track/nope123/')))

    def test_two_campaigns_to_same_event_get_distinct_links(self):
        """Per-campaign attribution: each saved campaign mints its own TrackingLink
        from the shared event link, so their tickets/revenue don't pool together."""
        import re as _re
        from .models import TrackingLink
        make_customer(self.org, 'vip@x.com', '+13105550009', rfm_segment='VIP')
        shared = self._link()  # the composer's shared per-event 'SMS' link
        body = f'Tickets https://test.cueup.co/track/{shared.token}/'

        def post(name):
            with self.captureOnCommitCallbacks(execute=True):
                return self.client.post(reverse('tickets:sms_campaign_create'), {
                    'name': name, 'rfm_segment': 'VIP', 'body': body,
                    'send_mode': 'now', 'confirm': '1',
                })

        self.assertEqual(post('Camp A').status_code, 302)
        self.assertEqual(post('Camp B').status_code, 302)
        a = SMSCampaign.objects.get(name='Camp A')
        b = SMSCampaign.objects.get(name='Camp B')
        ta = _re.search(r'/t/([A-Za-z0-9]+)/', a.link_url).group(1)
        tb = _re.search(r'/t/([A-Za-z0-9]+)/', b.link_url).group(1)
        # Each campaign got its own fresh token, distinct from each other + the shared one.
        self.assertEqual(len({ta, tb, shared.token}), 3)
        self.assertEqual(TrackingLink.objects.get(token=ta).name, 'SMS · Camp A')
        self.assertEqual(TrackingLink.objects.get(token=tb).name, 'SMS · Camp B')
        # Sales attribute independently per campaign.
        self._completed_session(TrackingLink.objects.get(token=ta), 2, 5000, 500)
        self._completed_session(TrackingLink.objects.get(token=tb), 1, 3000, 0)
        self.assertEqual(self._stats(a)['tickets'], 2)
        self.assertEqual(self._stats(a)['revenue'], Decimal('45.00'))
        self.assertEqual(self._stats(b)['tickets'], 1)
        self.assertEqual(self._stats(b)['revenue'], Decimal('30.00'))


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


@override_settings(E2E_TEST_MODE=True)
class SMSTicketLinkTests(TestCase):
    """Composer's 'Add a ticket link' dropdown + get-or-create tracked link endpoint."""

    def setUp(self):
        from datetime import date
        from .models import Venue, Event
        self.client = Client()
        self.org = Organization.objects.create(
            name='Org', slug='org-tl', sms_marketing_enabled=True,
            sms_credit_balance_cents=100000,
        )
        self.user = User.objects.create_user('tl', 'tl@test.com', 'pw')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        self.client.login(username='tl@test.com', password='pw')
        self.client.get(reverse('tickets:home'))
        self.venue = Venue.objects.create(organization=self.org, name='V', city='LA')
        future = date.today() + timedelta(days=30)
        self.live = Event.objects.create(
            organization=self.org, name='Rooftop Live', venue=self.venue,
            start_date=future, ticketing_type='direct', status='live',
        )
        self.draft = Event.objects.create(
            organization=self.org, name='Draft Show', venue=self.venue,
            start_date=future, ticketing_type='direct', status='draft',
        )
        self.external = Event.objects.create(
            organization=self.org, name='Eventbrite Show', venue=self.venue,
            start_date=future, ticketing_type='external', status='live',
        )
        self.url = reverse('tickets:sms_ticket_link')

    def test_compose_lists_only_live_direct_events(self):
        resp = self.client.get(reverse('tickets:sms_campaign_create'))
        self.assertEqual(resp.status_code, 200)
        ids = [e['id'] for e in resp.context['ticket_link_events']]
        self.assertIn(str(self.live.id), ids)
        self.assertNotIn(str(self.draft.id), ids)
        self.assertNotIn(str(self.external.id), ids)
        self.assertContains(resp, 'id="ticketlink-section"')

    @override_settings(SITE_URL='https://example.ngrok.app')
    def test_endpoint_creates_and_returns_track_url(self):
        from .models import TrackingLink
        resp = self.client.post(self.url, {'event': str(self.live.id)})
        self.assertEqual(resp.status_code, 200)
        link = TrackingLink.objects.get(organization=self.org, event=self.live, name='SMS')
        # URL must use SITE_URL (the public/tunnel host), not the request host,
        # so the link stored in the body resolves for recipients.
        self.assertEqual(
            resp.json()['url'], 'https://example.ngrok.app/t/' + link.token + '/',
        )

    def test_endpoint_idempotent(self):
        from .models import TrackingLink
        self.client.post(self.url, {'event': str(self.live.id)})
        self.client.post(self.url, {'event': str(self.live.id)})
        self.assertEqual(
            TrackingLink.objects.filter(organization=self.org, event=self.live, name='SMS').count(), 1,
        )

    def test_non_live_direct_event_400(self):
        resp = self.client.post(self.url, {'event': str(self.draft.id)})
        self.assertEqual(resp.status_code, 400)

    def test_non_direct_event_404(self):
        resp = self.client.post(self.url, {'event': str(self.external.id)})
        self.assertEqual(resp.status_code, 404)

    def test_foreign_event_404(self):
        from datetime import date
        from .models import Venue, Event
        other = Organization.objects.create(name='B', slug='org-tlb', sms_marketing_enabled=True)
        ov = Venue.objects.create(organization=other, name='V', city='SF')
        oe = Event.objects.create(
            organization=other, name='Theirs', venue=ov,
            start_date=date.today() + timedelta(days=10), ticketing_type='direct', status='live',
        )
        resp = self.client.post(self.url, {'event': str(oe.id)})
        self.assertEqual(resp.status_code, 404)

    def test_non_host_forbidden(self):
        doorman = User.objects.create_user('tldoor', 'tldoor@test.com', 'pw')
        UserProfile.objects.create(user=doorman, organization=self.org, org_role=UserProfile.OrgRole.DOORMAN)
        from .models import OrganizationMembership
        OrganizationMembership.objects.create(user=doorman, organization=self.org, org_role=UserProfile.OrgRole.DOORMAN)
        c = Client(); c.login(username='tldoor@test.com', password='pw'); c.get(reverse('tickets:home'))
        resp = c.post(self.url, {'event': str(self.live.id)})
        self.assertEqual(resp.status_code, 403)

    def test_sms_disabled_404(self):
        self.org.sms_marketing_enabled = False
        self.org.save(update_fields=['sms_marketing_enabled'])
        resp = self.client.post(self.url, {'event': str(self.live.id)})
        self.assertEqual(resp.status_code, 404)
