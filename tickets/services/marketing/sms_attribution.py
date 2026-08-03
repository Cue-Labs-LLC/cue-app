"""First-party UTM attribution: match native ticket orders to SlickText broadcasts.

SlickText auto-populates utm_id (campaign id), utm_campaign (campaign name), and
utm_source=SlickText on every outbound link. Cue captures those onto each ticket
order's `attribution` at checkout. This service computes a Cue-tracked, event-scoped
orders/revenue figure from that data and writes it onto the matching EventSMSCampaign
rows (cue_attributed_orders/revenue).

Mirrors services/marketing/utm_attribution.py (the Meta Ads equivalent). See
effective_orders/effective_revenue on EventSMSCampaign for the manual -> cue ->
slicktext priority chain that consumes these values.
"""

from datetime import timedelta
from decimal import Decimal

from django.conf import settings

from tickets.models import EventSMSCampaign, TicketOrder

# Recipient statuses that mean the message was actually handed to the carrier
# (mirrors _HANDED_OFF in sms_views; redefined here to avoid importing a view module).
_HANDED_OFF = ['sent', 'delivered', 'undelivered']


def _bump_marketing_cache(org_pk):
    """Mirror views._invalidate_marketing_cache without importing views (circular)."""
    from django.core.cache import cache as django_cache
    key = f'marketing_overview_ver:{org_pk}'
    try:
        django_cache.incr(key)
    except ValueError:
        try:
            django_cache.set(key, 1, timeout=None)
        except Exception:
            pass
    except Exception:
        pass


class SMSAttributionCalculator:
    """Recompute Cue-tracked attribution for an organization's SlickText broadcasts."""

    def __init__(self, organization):
        self.organization = organization

    def recompute_event(self, event):
        """Recompute cue_attributed_* for one event's SlickText broadcasts.

        Returns True if any campaign row changed. Applies the event-level fallback
        rule: if no order for the event carries SlickText UTM data, leave cue_* = None
        on every campaign so the table falls back to SlickText's (or manual) number.
        """
        campaigns = list(
            EventSMSCampaign.objects.filter(
                event=event,
                source='slicktext',
                deleted_at__isnull=True,
            ).exclude(external_id='')
        )
        if not campaigns:
            return False

        orders = list(
            TicketOrder.objects.filter(
                event=event,
                refunded_at__isnull=True,
            ).only('total_amount', 'attribution')
        )
        attributed_orders = [o for o in orders if o.attribution]

        # Only orders that came from SlickText count toward this channel.
        sms_orders = [o for o in attributed_orders if self._is_slicktext(o.attribution)]

        if not sms_orders:
            # Tracking not flowing for this event — clear Cue values, fall back to SlickText.
            return self._write(campaigns, {})

        # Build lookup keys for each campaign: external_id (== utm_id) and
        # name (== utm_campaign), both lowercased.
        by_key = {}
        for camp in campaigns:
            by_key[camp.external_id.lower()] = camp
            if camp.name:
                by_key.setdefault(camp.name.strip().lower(), camp)

        totals = {camp.id: {'orders': 0, 'revenue': Decimal('0.00')} for camp in campaigns}
        for order in sms_orders:
            camp = self._match(order.attribution, by_key)
            if camp is None:
                continue
            bucket = totals[camp.id]
            bucket['orders'] += 1
            bucket['revenue'] += order.total_amount or Decimal('0.00')

        return self._write(campaigns, totals)

    def recompute_all(self):
        """Recompute every event in the org that has at least one SlickText broadcast."""
        from tickets.models import Event
        event_ids = (
            EventSMSCampaign.objects.filter(
                event__organization=self.organization,
                source='slicktext',
                deleted_at__isnull=True,
            )
            .exclude(external_id='')
            .values_list('event_id', flat=True)
            .distinct()
        )
        changed = False
        for event in Event.objects.filter(id__in=list(event_ids)):
            if self.recompute_event(event):
                changed = True
        return changed

    @staticmethod
    def _is_slicktext(attribution):
        """Channel guard: only orders whose utm_source is SlickText count here."""
        return (attribution.get('utm_source') or '').strip().lower() == 'slicktext'

    @staticmethod
    def _match(attribution, by_key):
        """Resolve an order's attribution to a campaign: utm_id, then utm_campaign."""
        utm_id = (attribution.get('utm_id') or '').strip().lower()
        if utm_id and utm_id in by_key:
            return by_key[utm_id]
        utm_campaign = (attribution.get('utm_campaign') or '').strip().lower()
        if utm_campaign and utm_campaign in by_key:
            return by_key[utm_campaign]
        return None

    def _write(self, campaigns, totals):
        """Persist cue values; totals empty dict => set all to None (fallback)."""
        changed = False
        for camp in campaigns:
            if totals:
                bucket = totals[camp.id]
                new_orders = bucket['orders']
                new_revenue = bucket['revenue'].quantize(Decimal('0.01'))
            else:
                new_orders = None
                new_revenue = None
            if camp.cue_attributed_orders != new_orders or camp.cue_attributed_revenue != new_revenue:
                camp.cue_attributed_orders = new_orders
                camp.cue_attributed_revenue = new_revenue
                camp.version += 1
                camp.save(update_fields=['cue_attributed_orders', 'cue_attributed_revenue', 'version', 'updated_at'])
                changed = True
        if changed:
            _bump_marketing_cache(self.organization.pk)
        return changed


class NativeSMSAttributionCalculator:
    """Post-send conversion attribution for native (Cue) SMS campaigns.

    Unlike the SlickText path above, native campaigns carry a first-party link from each
    send to a customer (SMSMessageRecipient). We attribute an order to a campaign when the
    order's customer received that campaign's linked event send and bought within
    SMS_ATTRIBUTION_WINDOW_DAYS of the send. Overlapping campaigns are resolved last-touch
    (most recent qualifying send wins) so an order is never double-counted. Results are
    written to SMSCampaign.attributed_orders / attributed_revenue.
    """

    def __init__(self, organization):
        self.organization = organization

    def recompute_event(self, event):
        """Recompute attributed_orders/revenue for one event's native campaigns.

        Returns True if any campaign row changed. A campaign that has been through recompute
        always carries concrete values (0 when it drove no conversions), so the list shows
        "0" rather than "—" once tracking has run.
        """
        from tickets.models import SMSCampaign, SMSMessageRecipient

        campaigns = list(
            SMSCampaign.objects.filter(
                event=event,
                organization=self.organization,
                status=SMSCampaign.Status.SENT,
                deleted_at__isnull=True,
                sent_at__isnull=False,
            )
        )
        if not campaigns:
            return False

        campaign_ids = [c.id for c in campaigns]
        window = timedelta(days=getattr(settings, 'SMS_ATTRIBUTION_WINDOW_DAYS', 7))

        # Per-customer sends (recipient-level sent_at is more precise than the campaign's).
        sends_by_customer = {}
        recipient_rows = SMSMessageRecipient.objects.filter(
            campaign_id__in=campaign_ids,
            customer__isnull=False,
            sent_at__isnull=False,
            status__in=_HANDED_OFF,
        ).values('campaign_id', 'customer_id', 'sent_at')
        for row in recipient_rows:
            sends_by_customer.setdefault(row['customer_id'], []).append(
                (row['sent_at'], row['campaign_id'])
            )

        totals = {c.id: {'orders': 0, 'revenue': Decimal('0.00')} for c in campaigns}

        orders = TicketOrder.objects.filter(
            event=event,
            refunded_at__isnull=True,
        ).values('customer_id', 'order_date', 'total_amount')
        for order in orders:
            sends = sends_by_customer.get(order['customer_id'])
            if not sends:
                continue
            order_date = order['order_date']
            # Last-touch: most recent send that preceded the order within the window.
            best = None
            for sent_at, campaign_id in sends:
                if sent_at <= order_date <= sent_at + window:
                    if best is None or sent_at > best[0]:
                        best = (sent_at, campaign_id)
            if best is None:
                continue
            bucket = totals[best[1]]
            bucket['orders'] += 1
            bucket['revenue'] += order['total_amount'] or Decimal('0.00')

        return self._write(campaigns, totals)

    def recompute_all(self):
        """Recompute every event in the org that has at least one sent native campaign."""
        from tickets.models import Event, SMSCampaign

        event_ids = (
            SMSCampaign.objects.filter(
                organization=self.organization,
                status=SMSCampaign.Status.SENT,
                deleted_at__isnull=True,
                event__isnull=False,
            )
            .values_list('event_id', flat=True)
            .distinct()
        )
        changed = False
        for event in Event.objects.filter(id__in=list(event_ids)):
            if self.recompute_event(event):
                changed = True
        return changed

    def _write(self, campaigns, totals):
        """Persist attributed_orders/revenue; save only changed rows, bump cache once."""
        changed = False
        for camp in campaigns:
            bucket = totals[camp.id]
            new_orders = bucket['orders']
            new_revenue = bucket['revenue'].quantize(Decimal('0.01'))
            if camp.attributed_orders != new_orders or camp.attributed_revenue != new_revenue:
                camp.attributed_orders = new_orders
                camp.attributed_revenue = new_revenue
                camp.version += 1
                camp.save(update_fields=['attributed_orders', 'attributed_revenue', 'version', 'updated_at'])
                changed = True
        if changed:
            _bump_marketing_cache(self.organization.pk)
        return changed
