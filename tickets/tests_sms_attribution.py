from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from .models import (
    Customer,
    Event,
    EventSMSCampaign,
    Organization,
    TicketOrder,
    Venue,
)
from .services.marketing.sms_attribution import SMSAttributionCalculator


class EffectiveSMSAttributionChainTests(TestCase):
    def _campaign(self, **kwargs):
        org = Organization.objects.create(name='O', slug='o')
        venue = Venue.objects.create(organization=org, name='V', city='LA')
        event = Event.objects.create(organization=org, venue=venue, name='E', start_date=date.today())
        return EventSMSCampaign.objects.create(
            event=event, source='slicktext', external_id='cmp', name='Broadcast', **kwargs,
        )

    def test_chain_manual_beats_cue_beats_slicktext(self):
        camp = self._campaign(
            manual_orders=1, manual_revenue=Decimal('10.00'),
            cue_attributed_orders=2, cue_attributed_revenue=Decimal('20.00'),
            orders=3, revenue=Decimal('30.00'),
        )
        self.assertEqual(camp.effective_orders, 1)
        self.assertEqual(camp.effective_revenue, Decimal('10.00'))
        self.assertEqual(camp.attribution_source, 'manual')

    def test_cue_used_when_manual_none(self):
        camp = self._campaign(
            cue_attributed_orders=2, cue_attributed_revenue=Decimal('20.00'),
            orders=3, revenue=Decimal('30.00'),
        )
        self.assertEqual(camp.effective_orders, 2)
        self.assertEqual(camp.effective_revenue, Decimal('20.00'))
        self.assertEqual(camp.attribution_source, 'cue')

    def test_cue_zero_beats_slicktext(self):
        # Tracking active but no matches -> 0 wins over SlickText's own number.
        camp = self._campaign(
            cue_attributed_orders=0, cue_attributed_revenue=Decimal('0.00'),
            orders=3, revenue=Decimal('30.00'),
        )
        self.assertEqual(camp.effective_orders, 0)
        self.assertEqual(camp.effective_revenue, Decimal('0.00'))
        self.assertEqual(camp.attribution_source, 'cue')

    def test_slicktext_used_when_manual_and_cue_none(self):
        camp = self._campaign(orders=3, revenue=Decimal('30.00'))
        self.assertEqual(camp.effective_orders, 3)
        self.assertEqual(camp.effective_revenue, Decimal('30.00'))
        self.assertEqual(camp.attribution_source, 'slicktext')

    def test_zero_when_all_empty(self):
        camp = self._campaign()
        self.assertEqual(camp.effective_orders, 0)
        self.assertEqual(camp.effective_revenue, Decimal('0.00'))
        self.assertEqual(camp.attribution_source, 'none')


class SMSAttributionCalculatorTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Org', slug='org')
        self.venue = Venue.objects.create(organization=self.org, name='V', city='LA')
        self.event = Event.objects.create(
            organization=self.org, venue=self.venue, name='Show', start_date=date.today(),
        )
        self.customer = Customer.objects.create(organization=self.org, email='c@x.com', name='C')
        self.camp = EventSMSCampaign.objects.create(
            event=self.event, source='slicktext', external_id='st-99',
            name='Summer Promo', send_time=timezone.now(), confirmed_at=timezone.now(),
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
        self._order('40.00', {'utm_source': 'SlickText', 'utm_id': 'st-99'})
        self._order('60.00', {'utm_source': 'slicktext', 'utm_id': 'st-99'})
        changed = SMSAttributionCalculator(self.org).recompute_event(self.event)
        self.camp.refresh_from_db()
        self.assertTrue(changed)
        self.assertEqual(self.camp.cue_attributed_orders, 2)
        self.assertEqual(self.camp.cue_attributed_revenue, Decimal('100.00'))

    def test_match_by_utm_campaign_name_fallback(self):
        self._order('25.00', {'utm_source': 'slicktext', 'utm_campaign': 'summer promo'})
        SMSAttributionCalculator(self.org).recompute_event(self.event)
        self.camp.refresh_from_db()
        self.assertEqual(self.camp.cue_attributed_orders, 1)
        self.assertEqual(self.camp.cue_attributed_revenue, Decimal('25.00'))

    def test_non_slicktext_source_ignored(self):
        # Channel guard: a Meta order with a colliding utm_id must not count here.
        self._order('40.00', {'utm_source': 'facebook', 'utm_id': 'st-99'})
        SMSAttributionCalculator(self.org).recompute_event(self.event)
        self.camp.refresh_from_db()
        # No SlickText orders -> fall back to None (SlickText's own number shows).
        self.assertIsNone(self.camp.cue_attributed_orders)
        self.assertIsNone(self.camp.cue_attributed_revenue)

    def test_refunded_orders_excluded(self):
        self._order('40.00', {'utm_source': 'slicktext', 'utm_id': 'st-99'})
        self._order('99.00', {'utm_source': 'slicktext', 'utm_id': 'st-99'}, refunded=True)
        SMSAttributionCalculator(self.org).recompute_event(self.event)
        self.camp.refresh_from_db()
        self.assertEqual(self.camp.cue_attributed_orders, 1)
        self.assertEqual(self.camp.cue_attributed_revenue, Decimal('40.00'))

    def test_tagged_event_with_no_matches_writes_zero(self):
        # A SlickText order that matches no broadcast -> tracking is live, so the
        # campaign gets a concrete 0 (not None / SlickText fallback).
        self._order('40.00', {'utm_source': 'slicktext', 'utm_id': 'some-other-id'})
        SMSAttributionCalculator(self.org).recompute_event(self.event)
        self.camp.refresh_from_db()
        self.assertEqual(self.camp.cue_attributed_orders, 0)
        self.assertEqual(self.camp.cue_attributed_revenue, Decimal('0.00'))

    def test_no_sms_orders_leaves_none_for_fallback(self):
        # No order carries SlickText UTM data -> leave cue_* None so SlickText's
        # (or manual) number shows.
        self.camp.cue_attributed_orders = 5
        self.camp.cue_attributed_revenue = Decimal('50.00')
        self.camp.save(update_fields=['cue_attributed_orders', 'cue_attributed_revenue'])
        self._order('40.00', {})  # empty attribution
        SMSAttributionCalculator(self.org).recompute_event(self.event)
        self.camp.refresh_from_db()
        self.assertIsNone(self.camp.cue_attributed_orders)
        self.assertIsNone(self.camp.cue_attributed_revenue)

    def test_analytics_uses_cue_over_slicktext(self):
        from .services.marketing import MarketingAnalyticsService
        self.camp.orders = 99
        self.camp.revenue = Decimal('9999.00')
        self.camp.send_time = timezone.now() - timedelta(days=1)
        self.camp.save()
        self._order('120.00', {'utm_source': 'slicktext', 'utm_id': 'st-99'})
        SMSAttributionCalculator(self.org).recompute_event(self.event)

        result = MarketingAnalyticsService(self.org, window_days=90).calculate()
        self.assertEqual(result['channels']['sms']['revenue'], Decimal('120.00'))
        self.assertEqual(result['channels']['sms']['orders'], 1)
