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

from decimal import Decimal

from tickets.models import EventSMSCampaign, TicketOrder


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
