"""First-party UTM attribution: match native ticket orders to linked Meta campaigns.

Meta's Insights attribution is pixel-wide and person-based, so it credits a
campaign with purchases made for *other* events. This service computes a
Cue-tracked, event-scoped alternative from UTM params captured at checkout and
writes it onto the matching EventExpense rows (cue_attributed_orders/revenue).

See effective_attributed_* on EventExpense for the manual -> cue -> api -> 0
priority chain that consumes these values.
"""

from decimal import Decimal

from tickets.models import EventExpense, TicketOrder


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


class UTMAttributionCalculator:
    """Recompute Cue-tracked attribution for an organization's linked campaigns."""

    def __init__(self, organization):
        self.organization = organization

    def recompute_event(self, event):
        """Recompute cue_attributed_* for one event's linked Meta campaigns.

        Returns True if any expense row changed. Applies the event-level fallback
        rule: if no order for the event carries UTM data, leave cue_* = None on
        every linked campaign so the table falls back to Meta's API number.
        """
        expenses = list(
            EventExpense.objects.filter(
                event=event,
                source='meta_ads',
                deleted_at__isnull=True,
            ).exclude(external_id='')
        )
        if not expenses:
            return False

        orders = list(
            TicketOrder.objects.filter(
                event=event,
                refunded_at__isnull=True,
            ).only('total_amount', 'attribution')
        )
        attributed_orders = [o for o in orders if o.attribution]

        if not attributed_orders:
            # Tracking not flowing for this event — clear Cue values, fall back to Meta.
            return self._write(expenses, {})

        # Build lookup keys for each campaign: external_id (== utm_id) and
        # campaign name (== utm_campaign), both lowercased.
        by_key = {}
        for exp in expenses:
            by_key[exp.external_id.lower()] = exp
            campaign_name = (exp.external_metadata or {}).get('campaign_name')
            if campaign_name:
                by_key.setdefault(campaign_name.strip().lower(), exp)

        totals = {exp.id: {'orders': 0, 'revenue': Decimal('0.00')} for exp in expenses}
        for order in attributed_orders:
            exp = self._match(order.attribution, by_key)
            if exp is None:
                continue
            bucket = totals[exp.id]
            bucket['orders'] += 1
            bucket['revenue'] += order.total_amount or Decimal('0.00')

        return self._write(expenses, totals)

    def recompute_all(self):
        """Recompute every event in the org that has at least one linked campaign."""
        from tickets.models import Event
        event_ids = (
            EventExpense.objects.filter(
                event__organization=self.organization,
                source='meta_ads',
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
    def _match(attribution, by_key):
        """Resolve an order's attribution to a campaign: utm_id, then utm_campaign."""
        utm_id = (attribution.get('utm_id') or '').strip().lower()
        if utm_id and utm_id in by_key:
            return by_key[utm_id]
        utm_campaign = (attribution.get('utm_campaign') or '').strip().lower()
        if utm_campaign and utm_campaign in by_key:
            return by_key[utm_campaign]
        return None

    def _write(self, expenses, totals):
        """Persist cue values; totals empty dict => set all to None (fallback)."""
        changed = False
        for exp in expenses:
            if totals:
                bucket = totals[exp.id]
                new_orders = bucket['orders']
                new_revenue = bucket['revenue'].quantize(Decimal('0.01'))
            else:
                new_orders = None
                new_revenue = None
            if exp.cue_attributed_orders != new_orders or exp.cue_attributed_revenue != new_revenue:
                exp.cue_attributed_orders = new_orders
                exp.cue_attributed_revenue = new_revenue
                exp.version += 1
                exp.save(update_fields=['cue_attributed_orders', 'cue_attributed_revenue', 'version', 'updated_at'])
                changed = True
        if changed:
            _bump_marketing_cache(self.organization.pk)
        return changed
