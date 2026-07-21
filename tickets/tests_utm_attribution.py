from datetime import date, timedelta
from decimal import Decimal

from django.test import RequestFactory, TestCase
from django.utils import timezone

from .models import (
    Customer,
    Event,
    EventExpense,
    Organization,
    TicketOrder,
    Venue,
)
from .services.marketing.utm_attribution import UTMAttributionCalculator
from .views import _extract_utm_params


class ExtractUTMParamsTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_extracts_non_empty_params_fbclid_and_referrer(self):
        request = self.factory.get(
            '/e/abc/',
            {'utm_source': 'facebook', 'utm_id': '120', 'utm_medium': '', 'fbclid': 'fb123'},
            HTTP_REFERER='https://l.facebook.com/',
        )
        params = _extract_utm_params(request)
        self.assertEqual(params['utm_source'], 'facebook')
        self.assertEqual(params['utm_id'], '120')
        self.assertEqual(params['fbclid'], 'fb123')
        self.assertEqual(params['referrer'], 'https://l.facebook.com/')
        self.assertNotIn('utm_medium', params)  # empty dropped

    def test_no_params_returns_empty(self):
        request = self.factory.get('/e/abc/')
        self.assertEqual(_extract_utm_params(request), {})


class EffectiveAttributionChainTests(TestCase):
    def _expense(self, **kwargs):
        org = Organization.objects.create(name='O', slug='o')
        venue = Venue.objects.create(organization=org, name='V', city='LA')
        event = Event.objects.create(organization=org, venue=venue, name='E', start_date=date.today())
        return EventExpense.objects.create(
            event=event, category='marketing', description='Meta Ads',
            amount=Decimal('100.00'), source='meta_ads', external_id='cmp', **kwargs,
        )

    def test_chain_manual_beats_cue_beats_api(self):
        exp = self._expense(
            manual_attributed_orders=1, manual_attributed_revenue=Decimal('10.00'),
            cue_attributed_orders=2, cue_attributed_revenue=Decimal('20.00'),
            api_attributed_orders=3, api_attributed_revenue=Decimal('30.00'),
        )
        self.assertEqual(exp.effective_attributed_orders, 1)
        self.assertEqual(exp.effective_attributed_revenue, Decimal('10.00'))
        self.assertEqual(exp.attribution_source, 'manual')

    def test_cue_used_when_manual_none(self):
        exp = self._expense(
            cue_attributed_orders=2, cue_attributed_revenue=Decimal('20.00'),
            api_attributed_orders=3, api_attributed_revenue=Decimal('30.00'),
        )
        self.assertEqual(exp.effective_attributed_orders, 2)
        self.assertEqual(exp.effective_attributed_revenue, Decimal('20.00'))
        self.assertEqual(exp.attribution_source, 'cue')

    def test_cue_zero_beats_api(self):
        # Tracking active but no matches -> 0 wins over Meta's estimate.
        exp = self._expense(
            cue_attributed_orders=0, cue_attributed_revenue=Decimal('0.00'),
            api_attributed_orders=3, api_attributed_revenue=Decimal('30.00'),
        )
        self.assertEqual(exp.effective_attributed_orders, 0)
        self.assertEqual(exp.effective_attributed_revenue, Decimal('0.00'))
        self.assertEqual(exp.attribution_source, 'cue')

    def test_api_used_when_manual_and_cue_none(self):
        exp = self._expense(api_attributed_orders=3, api_attributed_revenue=Decimal('30.00'))
        self.assertEqual(exp.effective_attributed_orders, 3)
        self.assertEqual(exp.attribution_source, 'api')

    def test_zero_when_all_none(self):
        exp = self._expense()
        self.assertEqual(exp.effective_attributed_orders, 0)
        self.assertEqual(exp.effective_attributed_revenue, Decimal('0.00'))
        self.assertEqual(exp.attribution_source, 'none')


class UTMAttributionCalculatorTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Org', slug='org')
        self.venue = Venue.objects.create(organization=self.org, name='V', city='LA')
        self.event = Event.objects.create(
            organization=self.org, venue=self.venue, name='Show', start_date=date.today(),
        )
        self.customer = Customer.objects.create(organization=self.org, email='c@x.com', name='C')
        self.exp = EventExpense.objects.create(
            event=self.event, category='marketing', description='Meta Ads: Promo',
            amount=Decimal('500.00'), source='meta_ads', external_id='120555',
            external_metadata={'campaign_name': 'Summer Promo'},
            confirmed_at=timezone.now(),
        )
        self._n = 0

    def _order(self, amount, attribution, refunded=False):
        self._n += 1
        return TicketOrder.objects.create(
            customer=self.customer, event=self.event, uploaded_file=None,
            order_number=f'O-{self._n}', order_date=timezone.now(),
            total_amount=Decimal(amount), attribution=attribution,
            refunded_at=timezone.now() if refunded else None,
        )

    def test_match_by_utm_id(self):
        self._order('40.00', {'utm_id': '120555', 'utm_source': 'facebook'})
        self._order('60.00', {'utm_id': '120555'})
        changed = UTMAttributionCalculator(self.org).recompute_event(self.event)
        self.exp.refresh_from_db()
        self.assertTrue(changed)
        self.assertEqual(self.exp.cue_attributed_orders, 2)
        self.assertEqual(self.exp.cue_attributed_revenue, Decimal('100.00'))

    def test_match_by_utm_campaign_name_fallback(self):
        self._order('25.00', {'utm_campaign': 'summer promo'})  # case-insensitive
        UTMAttributionCalculator(self.org).recompute_event(self.event)
        self.exp.refresh_from_db()
        self.assertEqual(self.exp.cue_attributed_orders, 1)
        self.assertEqual(self.exp.cue_attributed_revenue, Decimal('25.00'))

    def test_refunded_orders_excluded(self):
        self._order('40.00', {'utm_id': '120555'})
        self._order('99.00', {'utm_id': '120555'}, refunded=True)
        UTMAttributionCalculator(self.org).recompute_event(self.event)
        self.exp.refresh_from_db()
        self.assertEqual(self.exp.cue_attributed_orders, 1)
        self.assertEqual(self.exp.cue_attributed_revenue, Decimal('40.00'))

    def test_tagged_event_with_no_matches_writes_zero(self):
        # An order carries UTM data but matches no campaign -> tracking is live,
        # so the campaign gets a concrete 0 (not None / Meta fallback).
        self._order('40.00', {'utm_id': 'some-other-campaign'})
        UTMAttributionCalculator(self.org).recompute_event(self.event)
        self.exp.refresh_from_db()
        self.assertEqual(self.exp.cue_attributed_orders, 0)
        self.assertEqual(self.exp.cue_attributed_revenue, Decimal('0.00'))

    def test_no_utm_orders_leaves_none_for_meta_fallback(self):
        # No order carries UTM data -> leave cue_* None so Meta's number shows.
        self.exp.cue_attributed_orders = 5
        self.exp.cue_attributed_revenue = Decimal('50.00')
        self.exp.save(update_fields=['cue_attributed_orders', 'cue_attributed_revenue'])
        self._order('40.00', {})  # empty attribution
        UTMAttributionCalculator(self.org).recompute_event(self.event)
        self.exp.refresh_from_db()
        self.assertIsNone(self.exp.cue_attributed_orders)
        self.assertIsNone(self.exp.cue_attributed_revenue)

    def test_analytics_uses_cue_over_api(self):
        from .services.marketing import MarketingAnalyticsService
        self.exp.api_attributed_revenue = Decimal('9999.00')
        self.exp.api_attributed_orders = 99
        self.exp.expense_date = date.today() - timedelta(days=1)
        self.exp.save()
        self._order('120.00', {'utm_id': '120555'})
        UTMAttributionCalculator(self.org).recompute_event(self.event)

        result = MarketingAnalyticsService(self.org, window_days=90).calculate()
        self.assertEqual(result['channels']['ads']['revenue'], Decimal('120.00'))
        self.assertEqual(result['channels']['ads']['orders'], 1)
