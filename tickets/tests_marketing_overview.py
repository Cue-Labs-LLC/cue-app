"""Event Marketing tab: per-channel Overview + compare-to-earlier-event, and the
slimmed Integrations view (only unconnected marketing providers)."""

from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .integrations.registry import unconnected_marketing_providers
from .models import (
    Customer,
    Event,
    EventEmailCampaign,
    EventExpense,
    EventSMSCampaign,
    MetaAdDailyInsight,
    Organization,
    OrganizationMembership,
    SMSCampaign,
    SMSMessageRecipient,
    UserProfile,
    Venue,
)
from .services.marketing.channel_overview import ChannelOverviewCalculator, stats_as_json


def _stats(overview):
    return {s['key']: s['value'] for s in overview['stats']}


class _MarketingFixture(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Org', slug='org')
        self.other_org = Organization.objects.create(name='Other', slug='other')
        self.venue = Venue.objects.create(organization=self.org, name='Hall', city='Oakland')
        self.other_venue = Venue.objects.create(organization=self.org, name='Club', city='Oakland')
        self.event = Event.objects.create(
            organization=self.org, venue=self.venue, name='Now', start_date=date(2026, 6, 1),
        )
        self.user = User.objects.create_user(username='owner', email='o@example.com', password='pass123')
        UserProfile.objects.create(user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER)
        OrganizationMembership.objects.create(
            user=self.user, organization=self.org, org_role=UserProfile.OrgRole.OWNER,
        )
        self.client.force_login(self.user)
        session = self.client.session
        session['_org_id'] = str(self.org.id)
        session.save()
        self.calc = ChannelOverviewCalculator(self.org)
        self._n = 0

    def _event(self, name, start, venue=None, org=None):
        org = org or self.org
        if venue is None:
            venue = self.venue if org == self.org else Venue.objects.get_or_create(
                organization=org, name='Elsewhere', defaults={'city': 'Reno'},
            )[0]
        return Event.objects.create(organization=org, venue=venue, name=name, start_date=start)

    def _native_send(self, event, recipients=(), status=SMSCampaign.Status.SENT, **extra):
        camp = SMSCampaign.objects.create(
            organization=event.organization, event=event, name='Blast', body='Tix', status=status, **extra,
        )
        for r_status, clicked, opted_out in recipients:
            self._n += 1
            customer = Customer.objects.create(
                organization=event.organization, email=f'c{self._n}@x.com', name=f'C{self._n}',
                phone=f'+120655501{self._n:02d}',
            )
            SMSMessageRecipient.objects.create(
                campaign=camp, customer=customer, phone=customer.phone, status=r_status,
                first_clicked_at=self.event_time() if clicked else None,
                opted_out_at=self.event_time() if opted_out else None,
            )
        return camp

    @staticmethod
    def event_time():
        from django.utils import timezone
        return timezone.now()

    def _broadcast(self, event, **fields):
        self._n += 1
        return EventSMSCampaign.objects.create(
            event=event, source='slicktext', external_id=f'st_{self._n}', name='Broadcast', **fields,
        )

    def _email(self, event, **fields):
        self._n += 1
        return EventEmailCampaign.objects.create(
            event=event, source='mailchimp', external_id=f'mc_{self._n}', campaign_title='Email', **fields,
        )


class SMSOverviewTests(_MarketingFixture):
    def test_combines_native_sends_and_slicktext(self):
        self._native_send(
            self.event,
            recipients=[('delivered', True, False), ('sent', False, True), ('failed', False, False)],
            attributed_orders=2, attributed_revenue=Decimal('80.00'),
        )
        # Drafts have no results yet and are ignored.
        self._native_send(self.event, recipients=[('queued', True, False)], status=SMSCampaign.Status.DRAFT)
        self._broadcast(
            self.event, audience_size=100, unique_clicks=10, unsubscribes=1,
            orders=3, revenue=Decimal('90.00'), manual_clicks=12,
        )

        overview = self.calc.calculate(self.event, 'sms')
        stats = _stats(overview)
        self.assertTrue(overview['has_data'])
        self.assertEqual(stats['sends'], 2)
        self.assertEqual(stats['recipients'], 102)       # 2 handed off + 100 audience
        self.assertEqual(stats['clicks'], 13)            # 1 native + manual override 12
        self.assertEqual(stats['unsubs'], 2)
        self.assertEqual(stats['ctr'], Decimal('12.7'))
        self.assertEqual(stats['orders'], 5)
        self.assertEqual(stats['revenue'], Decimal('170.00'))

    def test_no_attribution_hides_orders_and_revenue(self):
        self._native_send(self.event, recipients=[('delivered', False, False)])
        stats = _stats(self.calc.calculate(self.event, 'sms'))
        self.assertNotIn('orders', stats)
        self.assertNotIn('revenue', stats)

    def test_empty(self):
        overview = self.calc.calculate(self.event, 'sms')
        self.assertFalse(overview['has_data'])
        self.assertIsNone(_stats(overview)['ctr'])


class EmailOverviewTests(_MarketingFixture):
    def test_totals_respect_manual_overrides(self):
        self._email(self.event, emails_sent=1000, unique_opens=400, unique_clicks=50, unsubscribes=4,
                    ecommerce_orders=6, ecommerce_revenue=Decimal('300.00'), manual_unique_opens=500)
        self._email(self.event, emails_sent=1000, unique_opens=300, unique_clicks=30, unsubscribes=1)
        self._email(self.event, emails_sent=999, deleted_at=self.event_time())

        stats = _stats(self.calc.calculate(self.event, 'email'))
        self.assertEqual(stats['campaigns'], 2)
        self.assertEqual(stats['emails_sent'], 2000)
        self.assertEqual(stats['unique_opens'], 800)
        self.assertEqual(stats['open_rate'], Decimal('40.0'))
        self.assertEqual(stats['click_rate'], Decimal('4.0'))
        self.assertEqual(stats['unsubs'], 5)
        self.assertEqual(stats['orders'], 6)
        self.assertEqual(stats['revenue'], Decimal('300.00'))

    def test_stats_as_json_serialises_decimals(self):
        self._email(self.event, emails_sent=10, ecommerce_revenue=Decimal('12.50'))
        payload = stats_as_json(self.calc.calculate(self.event, 'email'))
        self.assertEqual(payload['revenue'], 12.5)
        self.assertEqual(payload['open_rate'], 0.0)
        self.assertEqual(payload['campaigns'], 1)


class CompareCandidateTests(_MarketingFixture):
    def test_only_earlier_events_with_channel_data_same_venue_first(self):
        same_old = self._event('Same venue old', date(2026, 1, 1))
        other_recent = self._event('Other venue recent', date(2026, 5, 1), venue=self.other_venue)
        same_recent = self._event('Same venue recent', date(2026, 4, 1))
        no_data = self._event('No email', date(2026, 5, 15))
        later = self._event('Later', date(2026, 7, 1))
        foreign = self._event('Foreign', date(2026, 5, 2), org=self.other_org)
        for e in (same_old, other_recent, same_recent, later, foreign):
            self._email(e)
        self._broadcast(no_data)

        ids = [c['id'] for c in self.calc.compare_candidates(self.event, 'email')]
        self.assertEqual(ids, [str(same_recent.id), str(same_old.id), str(other_recent.id)])

    def test_sms_candidates_ignore_unsent_native_campaigns(self):
        draft_only = self._event('Draft only', date(2026, 3, 1))
        sent = self._event('Sent', date(2026, 2, 1))
        broadcast = self._event('Broadcast', date(2026, 1, 1))
        self._native_send(draft_only, status=SMSCampaign.Status.DRAFT)
        self._native_send(sent)
        self._broadcast(broadcast)
        ids = [c['id'] for c in self.calc.compare_candidates(self.event, 'sms')]
        self.assertEqual(ids, [str(sent.id), str(broadcast.id)])

    def test_ads_candidates_need_synced_linked_campaigns(self):
        synced = self._event('Synced', date(2026, 3, 1))
        unsynced = self._event('Unsynced', date(2026, 4, 1))
        for e, cid in ((synced, 'cmp_synced'), (unsynced, 'cmp_unsynced')):
            EventExpense.objects.create(
                event=e, category='marketing', description='Ad', amount=Decimal('0.00'),
                source='meta_ads', external_id=cid,
            )
        MetaAdDailyInsight.objects.create(
            organization=self.org, ad_account_id='act_1', date=date(2026, 2, 20), ad_id='a1',
            campaign_id='cmp_synced', spend=Decimal('5.00'),
        )
        ids = [c['id'] for c in self.calc.compare_candidates(self.event, 'ads')]
        self.assertEqual(ids, [str(synced.id)])


class MarketingCompareApiTests(_MarketingFixture):
    def _url(self, **params):
        from urllib.parse import urlencode
        return reverse('tickets:event_marketing_compare_api', args=[self.event.id]) + '?' + urlencode(params)

    def test_returns_compare_event_stats(self):
        past = self._event('Past', date(2026, 1, 1))
        self._email(past, emails_sent=200, unique_opens=50)
        res = self.client.get(self._url(channel='email', compare_event=past.id))
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body['event_name'], 'Past')
        self.assertEqual(body['stats']['emails_sent'], 200)
        self.assertEqual(body['stats']['open_rate'], 25.0)

    def test_other_org_event_404(self):
        foreign = self._event('Foreign', date(2026, 1, 1), org=self.other_org)
        res = self.client.get(self._url(channel='email', compare_event=foreign.id))
        self.assertEqual(res.status_code, 404)

    def test_bad_channel_and_id_400(self):
        past = self._event('Past', date(2026, 1, 1))
        self.assertEqual(self.client.get(self._url(channel='tiktok', compare_event=past.id)).status_code, 400)
        self.assertEqual(self.client.get(self._url(channel='sms', compare_event='nope')).status_code, 400)

    def test_instagram_requires_feature_and_connection(self):
        past = self._event('Past', date(2026, 1, 1))
        res = self.client.get(self._url(channel='instagram', compare_event=past.id))
        self.assertEqual(res.status_code, 404)


class AdsEventCompareApiTests(_MarketingFixture):
    def test_aggregates_compare_events_linked_campaigns_with_days_before(self):
        past = self._event('Past', date(2026, 3, 10))
        for cid in ('c1', 'c2'):
            EventExpense.objects.create(
                event=past, category='marketing', description='Ad', amount=Decimal('0.00'),
                source='meta_ads', external_id=cid,
            )
        for i, cid in enumerate(('c1', 'c2', 'c_unlinked')):
            MetaAdDailyInsight.objects.create(
                organization=self.org, ad_account_id='act_1', date=date(2026, 3, 1), ad_id=f'a{i}',
                campaign_id=cid, spend=Decimal('10.00'), impressions=100, link_clicks=5,
            )
        url = reverse('tickets:event_ads_compare_api', args=[self.event.id])
        body = self.client.get(url, {'compare_event': str(past.id)}).json()
        self.assertEqual(body['name'], 'Past')
        self.assertEqual(len(body['days']), 1)
        self.assertEqual(body['days'][0]['spend'], 20.0)
        self.assertEqual(body['days'][0]['days_before'], 9)

    def test_other_org_event_404(self):
        foreign = self._event('Foreign', date(2026, 1, 1), org=self.other_org)
        url = reverse('tickets:event_ads_compare_api', args=[self.event.id])
        self.assertEqual(self.client.get(url, {'compare_event': str(foreign.id)}).status_code, 404)


class MarketingTabRenderTests(_MarketingFixture):
    def _page(self):
        return self.client.get(reverse('tickets:event_detail', kwargs={'event_id': self.event.id}))

    def test_email_view_and_overview_when_mailchimp_connected(self):
        self.org.mailchimp_access_token = 'tok'
        self.org.mailchimp_dc = 'us1'
        self.org.save()
        past = self._event('Past', date(2026, 1, 1))
        self._email(past, emails_sent=10)
        self._email(self.event, emails_sent=10)
        response = self._page()
        self.assertContains(response, 'data-marketing-view="email">Email</button>')
        self.assertContains(response, 'data-channel-overview="email"')
        self.assertContains(response, f'data-id="{past.id}"')
        # Mailchimp is now served by the Email view, so Integrations doesn't offer it.
        self.assertNotIn('mailchimp', [p['key'] for p in response.context['unconnected_marketing_providers']])

    def test_integrations_lists_only_unconnected_providers(self):
        self.org.slicktext_api_key = 'k'
        self.org.slicktext_brand_id = 'b'
        self.org.save()
        keys = [p['key'] for p in unconnected_marketing_providers(self.org)]
        self.assertIn('meta_ads', keys)
        self.assertIn('mailchimp', keys)
        self.assertNotIn('slicktext', keys)
        self.assertNotIn('typeform', keys)  # not a marketing channel
        response = self._page()
        self.assertContains(response, 'data-marketing-view="sms">SMS</button>')
        self.assertContains(response, 'data-marketing-view="linked">Integrations</button>')

    def test_integrations_hidden_when_everything_connected(self):
        self.org.slicktext_api_key = 'k'
        self.org.slicktext_brand_id = 'b'
        self.org.mailchimp_access_token = 'tok'
        self.org.mailchimp_dc = 'us1'
        self.org.meta_ads_account_id = 'act_1'
        self.org.meta_ads_access_token = 'tok'
        self.org.save()
        self.assertEqual(unconnected_marketing_providers(self.org), [])
        response = self._page()
        self.assertNotContains(response, 'data-marketing-view="linked"')
        self.assertNotContains(response, 'data-marketing-section="linked"')

    def test_meta_ads_card_lives_in_ads_view(self):
        self.org.meta_ads_account_id = 'act_1'
        self.org.meta_ads_access_token = 'tok'
        self.org.save()
        response = self._page()
        self.assertContains(response, 'data-marketing-view="ads">Ads</button>')
        html = response.content.decode()
        ads_start = html.index('data-marketing-section="ads"')
        ads_end = html.index('<!-- /Ads view -->')
        self.assertIn('id="metaAdsLinkedSection"', html[ads_start:ads_end])
        self.assertIn('No Meta Ads campaigns linked to this event yet.', html[ads_start:ads_end])

    def test_nothing_connected_renders_integrations_alone(self):
        self.org.sms_marketing_enabled = False
        self.org.save()
        response = self._page()
        self.assertNotContains(response, 'class="marketing-subnav"')
        self.assertContains(response, 'data-marketing-section="linked">')
        self.assertContains(response, 'Available integrations')
