"""Per-channel marketing Overview for the event detail Marketing tab.

Each Marketing channel view (SMS / Email / Instagram / Ads) opens with an Overview:
a row of stat cards summarising this event's results on that channel, plus a
"Compare vs <earlier event>" toggle. ``ChannelOverviewCalculator`` builds the stat
rows and the list of earlier events that have data on a channel (the compare
candidates). The compare API reuses the same calculator for the comparison event,
so both sides of a comparison are always computed identically.

Stats are returned as an ordered list of dicts:

    {key, label, value, raw, fmt, higher_is_better, variant, sub}

``value`` is a Decimal/int (or None when it can't be computed), ``raw`` is its
plain string form for the ``data-stat-value`` attribute the compare JS reads,
``fmt`` is one of count/money/pct/x, and ``higher_is_better`` drives the
green/red colouring of the compare delta.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Count, Q, Subquery, Sum

from ...models import (
    Event,
    EventEmailCampaign,
    EventExpense,
    EventSMSCampaign,
    InstagramPostEvent,
    MetaAdDailyInsight,
    SMSCampaign,
    SMSMessageRecipient,
)

CHANNELS = ('sms', 'email', 'instagram', 'ads')

# Native sends that actually went out (drafts / scheduled / canceled / failed
# campaigns have nothing to report yet).
NATIVE_SENT_STATUSES = (SMSCampaign.Status.SENDING, SMSCampaign.Status.SENT)
# Recipient statuses that were handed to the carrier (mirrors sms_views._HANDED_OFF).
HANDED_OFF_STATUSES = ('sent', 'delivered', 'undelivered')

CHANNEL_LABELS = {
    'sms': 'SMS',
    'email': 'email',
    'instagram': 'Instagram',
    'ads': 'ads',
}


def _rate(numerator, denominator):
    """Percentage rounded to one decimal, or None when the base is zero."""
    if not denominator or numerator is None:
        return None
    return (Decimal(numerator) * 100 / Decimal(denominator)).quantize(
        Decimal('0.1'), rounding=ROUND_HALF_UP,
    )


def _stat(key, label, value, fmt='count', higher_is_better=True, variant='', sub=''):
    return {
        'key': key,
        'label': label,
        'value': value,
        'raw': '' if value is None else str(value),
        'fmt': fmt,
        'higher_is_better': higher_is_better,
        'variant': variant,
        'sub': sub,
    }


def _n(value):
    """Thousands-separated count, or an em dash when missing."""
    return '\u2014' if value is None else f'{value:,}'


class ChannelOverviewCalculator:
    """Overview stats + compare candidates for one org's marketing channels."""

    def __init__(self, organization):
        self.organization = organization

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    def calculate(self, event, channel, instagram_totals=None):
        """Return ``{'has_data': bool, 'stats': [...]}`` for ``event`` on ``channel``.

        ``instagram_totals`` is the ``totals`` dict from
        ``EventInstagramContentCalculator`` (the Instagram data lives behind the
        Graph API, so the caller fetches it and passes it in).
        """
        if channel == 'sms':
            return self._sms(event)
        if channel == 'email':
            return self._email(event)
        if channel == 'instagram':
            return self._instagram(instagram_totals)
        raise ValueError(f'Unknown marketing channel: {channel}')

    def _sms(self, event):
        native = SMSCampaign.objects.filter(
            organization=self.organization,
            event=event,
            deleted_at__isnull=True,
            status__in=NATIVE_SENT_STATUSES,
        )
        native_counts = native.aggregate(
            sends=Count('id'),
            attributed_orders=Sum('attributed_orders'),
            attributed_revenue=Sum('attributed_revenue'),
        )
        recipient_counts = SMSMessageRecipient.objects.filter(campaign__in=native).aggregate(
            sent=Count('id', filter=Q(status__in=HANDED_OFF_STATUSES)),
            clicks=Count('id', filter=Q(first_clicked_at__isnull=False)),
            unsubs=Count('id', filter=Q(opted_out_at__isnull=False)),
        )
        broadcasts = list(
            EventSMSCampaign.objects.filter(
                event=event,
                event__organization=self.organization,
                deleted_at__isnull=True,
            )
        )

        sends = native_counts['sends'] + len(broadcasts)
        recipients = recipient_counts['sent'] + sum(b.effective_audience or 0 for b in broadcasts)
        clicks = recipient_counts['clicks'] + sum(b.effective_clicks or 0 for b in broadcasts)
        unsubs = recipient_counts['unsubs'] + sum(b.effective_unsubscribes or 0 for b in broadcasts)

        # Native attribution is NULL until first computed; SlickText always has a value.
        has_attribution = bool(broadcasts) or native_counts['attributed_orders'] is not None
        orders = (native_counts['attributed_orders'] or 0) + sum(b.effective_orders or 0 for b in broadcasts)
        revenue = (native_counts['attributed_revenue'] or Decimal('0.00')) + sum(
            (b.effective_revenue or Decimal('0.00') for b in broadcasts), Decimal('0.00'),
        )

        stats = [
            _stat('sends', 'Sends', sends, variant='accent'),
            _stat('recipients', 'Recipients', recipients, variant='info'),
            _stat('clicks', 'Clicks', clicks, variant='info'),
            _stat('ctr', 'Click rate', _rate(clicks, recipients), fmt='pct'),
            _stat('unsubs', 'Unsubscribes', unsubs, higher_is_better=False),
        ]
        if has_attribution:
            stats += [
                _stat('orders', 'Orders', orders, variant='success'),
                _stat('revenue', 'Revenue', revenue, fmt='money', variant='success'),
            ]
        return {'has_data': sends > 0, 'stats': stats}

    def _email(self, event):
        campaigns = list(
            EventEmailCampaign.objects.filter(
                event=event,
                event__organization=self.organization,
                deleted_at__isnull=True,
            )
        )
        sent = sum(c.effective_emails_sent or 0 for c in campaigns)
        opens = sum(c.effective_unique_opens or 0 for c in campaigns)
        clicks = sum(c.effective_clicks or 0 for c in campaigns)
        unsubs = sum(c.effective_unsubscribes or 0 for c in campaigns)
        orders = sum(c.effective_orders or 0 for c in campaigns)
        revenue = sum((c.effective_revenue or Decimal('0.00') for c in campaigns), Decimal('0.00'))
        stats = [
            _stat('campaigns', 'Campaigns', len(campaigns), variant='accent'),
            _stat('emails_sent', 'Emails sent', sent, variant='info'),
            _stat('unique_opens', 'Unique opens', opens, variant='info'),
            _stat('open_rate', 'Open rate', _rate(opens, sent), fmt='pct'),
            _stat('clicks', 'Clicks', clicks, variant='info'),
            _stat('click_rate', 'Click rate', _rate(clicks, sent), fmt='pct'),
            _stat('unsubs', 'Unsubscribes', unsubs, higher_is_better=False),
            _stat('orders', 'Orders', orders, variant='success'),
            _stat('revenue', 'Revenue', revenue, fmt='money', variant='success'),
        ]
        return {'has_data': bool(campaigns), 'stats': stats}

    def _instagram(self, totals):
        totals = totals or {}
        median_index = totals.get('median_reach_index')
        engagement = totals.get('engagement_rate')
        views = totals.get('views')
        stats = [
            _stat('posts', 'Posts', totals.get('posts', 0), variant='accent',
                  sub=' \u00b7 '.join(f'{n} {label.lower()}' for label, n in totals.get('formats', []))),
            _stat('reach', 'Total reach', totals.get('reach'), variant='info',
                  sub=f'{views:,} views' if views is not None else 'Summed per post'),
            _stat('engagement_rate', 'Engagement rate',
                  None if engagement is None else Decimal(str(engagement)), fmt='pct',
                  sub=f"{_n(totals.get('interactions'))} interactions"),
            _stat('shares_saves', 'Shares & saves', totals.get('shares_saves'),
                  sub=f"{_n(totals.get('shares'))} shares \u00b7 {_n(totals.get('saves'))} saves"),
            _stat('median_reach_index', 'vs typical post',
                  None if median_index is None else Decimal(str(median_index)), fmt='x',
                  sub='Median linked post'),
        ]
        if totals.get('has_feed_posts'):
            stats.append(_stat('follows', 'Follows', totals.get('follows'), variant='success',
                               sub=f"{_n(totals.get('profile_visits'))} profile visits"))
        return {'has_data': bool(totals.get('posts')), 'stats': stats}

    # ------------------------------------------------------------------
    # Compare candidates
    # ------------------------------------------------------------------
    def _channel_filter(self, channel):
        """Q over Event selecting events that have data on ``channel``."""
        if channel == 'sms':
            native_event_ids = SMSCampaign.objects.filter(
                organization=self.organization,
                deleted_at__isnull=True,
                status__in=NATIVE_SENT_STATUSES,
                event__isnull=False,
            ).values('event_id')
            broadcast_event_ids = EventSMSCampaign.objects.filter(
                event__organization=self.organization, deleted_at__isnull=True,
            ).values('event_id')
            return Q(id__in=Subquery(native_event_ids)) | Q(id__in=Subquery(broadcast_event_ids))
        if channel == 'email':
            return Q(id__in=Subquery(
                EventEmailCampaign.objects.filter(
                    event__organization=self.organization, deleted_at__isnull=True,
                ).values('event_id')
            ))
        if channel == 'instagram':
            return Q(id__in=Subquery(
                InstagramPostEvent.objects.filter(organization=self.organization).values('event_id')
            ))
        if channel == 'ads':
            synced_campaign_ids = MetaAdDailyInsight.objects.filter(
                organization=self.organization,
            ).values('campaign_id')
            return Q(id__in=Subquery(
                EventExpense.objects.filter(
                    event__organization=self.organization,
                    source='meta_ads',
                    deleted_at__isnull=True,
                    external_id__in=Subquery(synced_campaign_ids),
                ).values('event_id')
            ))
        raise ValueError(f'Unknown marketing channel: {channel}')

    def compare_candidates(self, event, channel, limit=100):
        """Earlier events with ``channel`` data, most recent first, same venue first.

        Mirrors ``_get_pacing_comparison_candidates``: the same venue is the fairest
        comparison, so those float to the top and the first item is the default.
        """
        past = list(
            Event.objects.filter(organization=self.organization, start_date__lt=event.start_date)
            .filter(self._channel_filter(channel))
            .exclude(id=event.id)
            .only('id', 'name', 'start_date', 'venue_id')
            .order_by('-start_date')[:limit]
        )
        rows = [
            {
                'id': str(e.id),
                'name': e.name,
                'start_date': e.start_date,
                'is_same_venue': bool(event.venue_id and e.venue_id == event.venue_id),
            }
            for e in past
        ]
        same_venue = [r for r in rows if r['is_same_venue']]
        other = [r for r in rows if not r['is_same_venue']]
        return same_venue + other


def stats_as_json(overview):
    """``{key: float|None}`` for the compare API response."""
    return {
        s['key']: (None if s['value'] is None else float(s['value']))
        for s in overview['stats']
    }
