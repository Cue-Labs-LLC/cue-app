from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from .models import (
    Customer,
    Event,
    Organization,
    SMSCampaign,
    SMSMessageRecipient,
    TicketOrder,
    Venue,
)
from .services.marketing.sms_attribution import NativeSMSAttributionCalculator


class NativeSMSAttributionCalculatorTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Org', slug='org')
        self.venue = Venue.objects.create(organization=self.org, name='V', city='LA')
        self.event = Event.objects.create(
            organization=self.org, venue=self.venue, name='Show', start_date=date.today(),
        )
        self.send_at = timezone.now() - timedelta(days=2)
        self.camp = SMSCampaign.objects.create(
            organization=self.org, event=self.event, name='Blast', body='Tix live',
            link_url='https://cueup.co/e', status=SMSCampaign.Status.SENT,
            sent_at=self.send_at, audience_size=2,
        )
        self._c = 0
        self._o = 0

    def _customer(self):
        self._c += 1
        return Customer.objects.create(
            organization=self.org, email=f'c{self._c}@x.com', name=f'C{self._c}',
            phone=f'+1206555010{self._c}',
        )

    def _recipient(self, customer, campaign=None, sent_at=None, status='delivered'):
        return SMSMessageRecipient.objects.create(
            campaign=campaign or self.camp, customer=customer, phone=customer.phone,
            status=status, sent_at=sent_at or self.send_at,
        )

    def _order(self, customer, amount, order_date, refunded=False):
        self._o += 1
        return TicketOrder.objects.create(
            customer=customer, event=self.event, uploaded_file=None,
            order_number=f'O-{self._o}', order_date=order_date,
            total_amount=Decimal(amount),
            refunded_at=timezone.now() if refunded else None,
        )

    def test_post_send_purchase_within_window_attributed(self):
        cust = self._customer()
        self._recipient(cust)
        self._order(cust, '40.00', self.send_at + timedelta(days=1))
        changed = NativeSMSAttributionCalculator(self.org).recompute_event(self.event)
        self.camp.refresh_from_db()
        self.assertTrue(changed)
        self.assertEqual(self.camp.attributed_orders, 1)
        self.assertEqual(self.camp.attributed_revenue, Decimal('40.00'))

    def test_purchase_outside_window_not_attributed(self):
        cust = self._customer()
        self._recipient(cust)
        self._order(cust, '40.00', self.send_at + timedelta(days=10))
        NativeSMSAttributionCalculator(self.org).recompute_event(self.event)
        self.camp.refresh_from_db()
        # Computed but no qualifying conversion -> concrete 0 (shows "0", not "—").
        self.assertEqual(self.camp.attributed_orders, 0)
        self.assertEqual(self.camp.attributed_revenue, Decimal('0.00'))

    def test_purchase_before_send_not_attributed(self):
        cust = self._customer()
        self._recipient(cust)
        self._order(cust, '40.00', self.send_at - timedelta(hours=1))
        NativeSMSAttributionCalculator(self.org).recompute_event(self.event)
        self.camp.refresh_from_db()
        self.assertEqual(self.camp.attributed_orders, 0)

    def test_non_recipient_purchase_not_attributed(self):
        buyer = self._customer()  # never received the campaign
        self._order(buyer, '40.00', self.send_at + timedelta(days=1))
        NativeSMSAttributionCalculator(self.org).recompute_event(self.event)
        self.camp.refresh_from_db()
        self.assertEqual(self.camp.attributed_orders, 0)

    def test_refunded_order_excluded(self):
        cust = self._customer()
        self._recipient(cust)
        self._order(cust, '40.00', self.send_at + timedelta(days=1))
        self._order(cust, '99.00', self.send_at + timedelta(days=1), refunded=True)
        NativeSMSAttributionCalculator(self.org).recompute_event(self.event)
        self.camp.refresh_from_db()
        self.assertEqual(self.camp.attributed_orders, 1)
        self.assertEqual(self.camp.attributed_revenue, Decimal('40.00'))

    def test_queued_recipient_not_counted(self):
        # A recipient whose message never reached the carrier (still 'queued') is not a send.
        cust = self._customer()
        self._recipient(cust, status='queued')
        self._order(cust, '40.00', self.send_at + timedelta(days=1))
        NativeSMSAttributionCalculator(self.org).recompute_event(self.event)
        self.camp.refresh_from_db()
        self.assertEqual(self.camp.attributed_orders, 0)

    def test_last_touch_credits_most_recent_send(self):
        # Customer received two overlapping campaigns; the order is credited only to the
        # later send, never both.
        later = SMSCampaign.objects.create(
            organization=self.org, event=self.event, name='Blast 2', body='Last chance',
            status=SMSCampaign.Status.SENT, sent_at=self.send_at + timedelta(days=1),
        )
        cust = self._customer()
        self._recipient(cust, campaign=self.camp, sent_at=self.send_at)
        self._recipient(cust, campaign=later, sent_at=self.send_at + timedelta(days=1))
        self._order(cust, '50.00', self.send_at + timedelta(days=1, hours=2))
        NativeSMSAttributionCalculator(self.org).recompute_event(self.event)
        self.camp.refresh_from_db()
        later.refresh_from_db()
        self.assertEqual(self.camp.attributed_orders, 0)
        self.assertEqual(later.attributed_orders, 1)
        self.assertEqual(later.attributed_revenue, Decimal('50.00'))

    def test_no_sent_campaigns_returns_false(self):
        SMSCampaign.objects.update(status=SMSCampaign.Status.DRAFT)
        changed = NativeSMSAttributionCalculator(self.org).recompute_event(self.event)
        self.assertFalse(changed)

    def test_recompute_all_covers_event(self):
        cust = self._customer()
        self._recipient(cust)
        self._order(cust, '40.00', self.send_at + timedelta(days=1))
        changed = NativeSMSAttributionCalculator(self.org).recompute_all()
        self.camp.refresh_from_db()
        self.assertTrue(changed)
        self.assertEqual(self.camp.attributed_orders, 1)
