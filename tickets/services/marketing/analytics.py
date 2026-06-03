"""Org-wide marketing analytics aggregations across Meta Ads, Mailchimp, SlickText."""

from collections import OrderedDict
from datetime import timedelta
from decimal import Decimal

from django.db.models import (
    Count,
    DecimalField,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone

from tickets.models import (
    Event,
    EventEmailCampaign,
    EventExpense,
    EventSMSCampaign,
)


WINDOW_CHOICES = OrderedDict([
    ('30', {'label': 'Last 30 days', 'days': 30}),
    ('90', {'label': 'Last 90 days', 'days': 90}),
    ('365', {'label': 'Last 12 months', 'days': 365}),
    ('all', {'label': 'All time', 'days': None}),
])

DEFAULT_WINDOW = '90'

ZERO = Decimal('0.00')


def resolve_window(value):
    """Normalize a window querystring value, returning (key, days, label)."""
    key = value if value in WINDOW_CHOICES else DEFAULT_WINDOW
    info = WINDOW_CHOICES[key]
    return key, info['days'], info['label']


def _safe_div(numer, denom):
    if not denom:
        return Decimal('0.0000')
    return (Decimal(numer) / Decimal(denom)).quantize(Decimal('0.0001'))


class MarketingAnalyticsService:
    """Aggregate marketing performance across channels for one organization."""

    def __init__(self, organization, window_days):
        self.organization = organization
        self.window_days = window_days
        now = timezone.now()
        self.window_end = now
        self.window_start = now - timedelta(days=window_days) if window_days else None

    def calculate(self):
        email_totals = self._email_totals()
        sms_totals = self._sms_totals()
        ads_totals = self._ads_totals()

        channels = {
            'email': self._format_email(email_totals),
            'sms': self._format_sms(sms_totals),
            'ads': self._format_ads(ads_totals),
        }

        trends = self._trends()
        engagement_trends = self._engagement_trends()
        channel_comparison = self._channel_comparison(channels)
        top_email = self._top_email_campaigns()
        top_sms = self._top_sms_campaigns()
        top_events = self._top_events_by_roi()

        return {
            'channels': channels,
            'trends': trends,
            'engagement_trends': engagement_trends,
            'channel_comparison': channel_comparison,
            'top_email_campaigns': top_email,
            'top_sms_campaigns': top_sms,
            'top_campaigns': top_email + top_sms,
            'top_events_by_roi': top_events,
            'native_sms': self._native_sms_summary(),
            'meta': {
                'window_start': self.window_start.isoformat() if self.window_start else None,
                'window_end': self.window_end.isoformat(),
            },
        }

    def _native_sms_summary(self):
        """Send-side stats for native marketing SMS (this app's own sends).

        Distinct from `_sms_totals`, which tracks *revenue* synced from external
        SlickText campaigns. Native sends have no revenue attribution yet (see
        TODOS.md), so we surface delivery + opt-out counts where users expect
        them: the marketing SMS tab.
        """
        from tickets.models import SMSCampaign, SMSMessageRecipient, PhoneSuppression

        campaigns = SMSCampaign.objects.filter(
            organization=self.organization,
            deleted_at__isnull=True,
            status=SMSCampaign.Status.SENT,
        )
        if self.window_start is not None:
            campaigns = campaigns.filter(sent_at__gte=self.window_start)

        recipients = SMSMessageRecipient.objects.filter(campaign__in=campaigns)
        recipient_stats = recipients.aggregate(
            sent=Count('id', filter=Q(status__in=['sent', 'delivered', 'undelivered'])),
            delivered=Count('id', filter=Q(status='delivered')),
            failed=Count('id', filter=Q(status__in=['failed', 'undelivered'])),
            unique_clicks=Count('id', filter=Q(first_clicked_at__isnull=False)),
            total_clicks=Coalesce(Sum('click_count'), 0),
            unsubscribes=Count('id', filter=Q(opted_out_at__isnull=False)),
        )

        # Global opt-outs (Twilio STOP) in the window — superset of campaign-attributed
        # unsubscribes, since a STOP can arrive without matching a recent send.
        opt_outs = PhoneSuppression.objects.filter(
            Q(organization=self.organization) | Q(organization__isnull=True),
        )
        if self.window_start is not None:
            opt_outs = opt_outs.filter(created_at__gte=self.window_start)

        sent = recipient_stats['sent'] or 0
        delivered = recipient_stats['delivered'] or 0
        unique_clicks = recipient_stats['unique_clicks'] or 0
        return {
            'campaigns_sent': campaigns.count(),
            'messages_sent': sent,
            'messages_delivered': delivered,
            'messages_failed': recipient_stats['failed'] or 0,
            'delivery_rate': _safe_div(delivered, sent),
            'total_clicks': recipient_stats['total_clicks'] or 0,
            'unique_clicks': unique_clicks,
            'click_rate': _safe_div(unique_clicks, delivered),
            'unsubscribes': recipient_stats['unsubscribes'] or 0,
            'opt_outs': opt_outs.count(),
        }

    def _email_qs(self):
        qs = EventEmailCampaign.objects.filter(
            event__organization=self.organization,
            event__deleted_at__isnull=True,
            deleted_at__isnull=True,
            confirmed_at__isnull=False,
        )
        if self.window_start is not None:
            qs = qs.filter(send_time__gte=self.window_start)
        return qs

    def _sms_qs(self):
        qs = EventSMSCampaign.objects.filter(
            event__organization=self.organization,
            event__deleted_at__isnull=True,
            deleted_at__isnull=True,
            confirmed_at__isnull=False,
        )
        if self.window_start is not None:
            qs = qs.filter(send_time__gte=self.window_start)
        return qs

    def _ads_qs(self):
        qs = EventExpense.objects.filter(
            event__organization=self.organization,
            event__deleted_at__isnull=True,
            source='meta_ads',
            deleted_at__isnull=True,
            confirmed_at__isnull=False,
        )
        if self.window_start is not None:
            qs = qs.filter(expense_date__gte=self.window_start.date())
        return qs

    @staticmethod
    def _coalesce_int(manual_field, api_field):
        return Coalesce(F(manual_field), F(api_field), output_field=IntegerField())

    @staticmethod
    def _coalesce_decimal(manual_field, api_field):
        return Coalesce(F(manual_field), F(api_field), output_field=DecimalField(max_digits=12, decimal_places=2))

    def _email_totals(self):
        return self._email_qs().annotate(
            eff_sends=self._coalesce_int('manual_emails_sent', 'emails_sent'),
            eff_opens=self._coalesce_int('manual_unique_opens', 'unique_opens'),
            eff_clicks=self._coalesce_int('manual_clicks', 'unique_clicks'),
            eff_unsubs=self._coalesce_int('manual_unsubscribes', 'unsubscribes'),
            eff_orders=self._coalesce_int('manual_orders', 'ecommerce_orders'),
            eff_revenue=self._coalesce_decimal('manual_revenue', 'ecommerce_revenue'),
        ).aggregate(
            campaigns=Count('id'),
            sends=Coalesce(Sum('eff_sends'), 0),
            opens=Coalesce(Sum('eff_opens'), 0),
            clicks=Coalesce(Sum('eff_clicks'), 0),
            unsubscribes=Coalesce(Sum('eff_unsubs'), 0),
            orders=Coalesce(Sum('eff_orders'), 0),
            revenue=Coalesce(Sum('eff_revenue'), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2)),
        )

    def _sms_totals(self):
        return self._sms_qs().annotate(
            eff_audience=self._coalesce_int('manual_audience', 'audience_size'),
            eff_clicks=self._coalesce_int('manual_clicks', 'unique_clicks'),
            eff_unsubs=self._coalesce_int('manual_unsubscribes', 'unsubscribes'),
            eff_orders=self._coalesce_int('manual_orders', 'orders'),
            eff_revenue=self._coalesce_decimal('manual_revenue', 'revenue'),
        ).aggregate(
            campaigns=Count('id'),
            audience=Coalesce(Sum('eff_audience'), 0),
            clicks=Coalesce(Sum('eff_clicks'), 0),
            unsubscribes=Coalesce(Sum('eff_unsubs'), 0),
            orders=Coalesce(Sum('eff_orders'), 0),
            revenue=Coalesce(Sum('eff_revenue'), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2)),
        )

    def _ads_totals(self):
        return self._ads_qs().aggregate(
            line_items=Count('id'),
            spend=Coalesce(Sum('amount'), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2)),
            attributed_orders=Coalesce(Sum('manual_attributed_orders'), 0),
            attributed_revenue=Coalesce(Sum('manual_attributed_revenue'), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2)),
        )

    def _format_email(self, totals):
        sends = totals['sends']
        return {
            'campaigns': totals['campaigns'],
            'sends': sends,
            'opens': totals['opens'],
            'clicks': totals['clicks'],
            'unsubscribes': totals['unsubscribes'],
            'orders': totals['orders'],
            'revenue': totals['revenue'],
            'open_rate': _safe_div(totals['opens'], sends),
            'click_rate': _safe_div(totals['clicks'], sends),
            'unsub_rate': _safe_div(totals['unsubscribes'], sends),
        }

    def _format_sms(self, totals):
        audience = totals['audience']
        return {
            'campaigns': totals['campaigns'],
            'audience': audience,
            'clicks': totals['clicks'],
            'unsubscribes': totals['unsubscribes'],
            'orders': totals['orders'],
            'revenue': totals['revenue'],
            'click_rate': _safe_div(totals['clicks'], audience),
            'unsub_rate': _safe_div(totals['unsubscribes'], audience),
        }

    def _format_ads(self, totals):
        spend = totals['spend']
        ads_revenue = totals['attributed_revenue']
        ads_orders = totals['attributed_orders']
        roas = (ads_revenue / spend) if spend else Decimal('0.0000')
        roi = ((ads_revenue - spend) / spend) if spend else Decimal('0.0000')
        return {
            'line_items': totals['line_items'],
            'spend': spend,
            'orders': ads_orders,
            'revenue': ads_revenue,
            'roas': roas.quantize(Decimal('0.0001')),
            'roi': roi.quantize(Decimal('0.0001')),
        }

    def _trends(self):
        """Monthly revenue + ads spend across the window for stacked bar chart."""
        months = {}

        def bucket(key):
            return months.setdefault(key, {
                'month': key,
                'email_revenue': ZERO,
                'sms_revenue': ZERO,
                'ads_spend': ZERO,
            })

        email_rows = (
            self._email_qs()
            .exclude(send_time__isnull=True)
            .annotate(
                m=TruncMonth('send_time'),
                eff_revenue=self._coalesce_decimal('manual_revenue', 'ecommerce_revenue'),
            )
            .values('m')
            .annotate(revenue=Coalesce(Sum('eff_revenue'), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2)))
            .order_by('m')
        )
        for row in email_rows:
            month_key = row['m'].date().isoformat() if row['m'] else None
            if month_key:
                bucket(month_key)['email_revenue'] = row['revenue']

        sms_rows = (
            self._sms_qs()
            .exclude(send_time__isnull=True)
            .annotate(
                m=TruncMonth('send_time'),
                eff_revenue=self._coalesce_decimal('manual_revenue', 'revenue'),
            )
            .values('m')
            .annotate(revenue=Coalesce(Sum('eff_revenue'), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2)))
            .order_by('m')
        )
        for row in sms_rows:
            month_key = row['m'].date().isoformat() if row['m'] else None
            if month_key:
                bucket(month_key)['sms_revenue'] = row['revenue']

        ads_rows = (
            self._ads_qs()
            .exclude(expense_date__isnull=True)
            .annotate(m=TruncMonth('expense_date'))
            .values('m')
            .annotate(spend=Coalesce(Sum('amount'), ZERO, output_field=DecimalField(max_digits=12, decimal_places=2)))
            .order_by('m')
        )
        for row in ads_rows:
            month_key = row['m'].isoformat() if row['m'] else None
            if month_key:
                bucket(month_key)['ads_spend'] = row['spend']

        return sorted(months.values(), key=lambda r: r['month'])

    def _channel_comparison(self, channels):
        email_rev = channels['email']['revenue']
        sms_rev = channels['sms']['revenue']
        ads_rev = channels['ads']['revenue']
        total = email_rev + sms_rev + ads_rev

        def share(amount):
            if not total:
                return Decimal('0.0000')
            return (Decimal(amount) / Decimal(total)).quantize(Decimal('0.0001'))

        return [
            {'channel': 'email', 'label': 'Email', 'revenue': email_rev, 'share': share(email_rev)},
            {'channel': 'sms', 'label': 'SMS', 'revenue': sms_rev, 'share': share(sms_rev)},
            {'channel': 'ads', 'label': 'Ads', 'revenue': ads_rev, 'share': share(ads_rev)},
        ]

    def _top_email_campaigns(self, limit=10):
        rows = []
        qs = (
            self._email_qs()
            .select_related('event')
            .annotate(
                eff_clicks=self._coalesce_int('manual_clicks', 'unique_clicks'),
                eff_orders=self._coalesce_int('manual_orders', 'ecommerce_orders'),
                eff_revenue=self._coalesce_decimal('manual_revenue', 'ecommerce_revenue'),
            )
            .values(
                'id', 'campaign_title', 'send_time',
                'emails_sent', 'unique_opens',
                'eff_clicks', 'eff_orders', 'eff_revenue',
                'event_id', 'event__name',
            )
            .order_by('-eff_revenue')[:limit]
        )
        for row in qs:
            sends = row['emails_sent'] or 0
            opens = row['unique_opens'] or 0
            clicks = row['eff_clicks'] or 0
            rows.append({
                'channel': 'email',
                'channel_label': 'Email',
                'name': row['campaign_title'] or 'Untitled campaign',
                'send_time': row['send_time'].isoformat() if row['send_time'] else None,
                'revenue': row['eff_revenue'] or ZERO,
                'orders': row['eff_orders'] or 0,
                'sends': sends,
                'opens': opens,
                'clicks': clicks,
                'open_rate': _safe_div(opens, sends),
                'click_rate': _safe_div(clicks, sends),
                'event_id': str(row['event_id']),
                'event_name': row['event__name'],
            })
        return rows

    def _top_sms_campaigns(self, limit=10):
        rows = []
        qs = (
            self._sms_qs()
            .select_related('event')
            .annotate(
                eff_clicks=self._coalesce_int('manual_clicks', 'unique_clicks'),
                eff_orders=self._coalesce_int('manual_orders', 'orders'),
                eff_revenue=self._coalesce_decimal('manual_revenue', 'revenue'),
            )
            .values(
                'id', 'name', 'send_time', 'audience_size',
                'eff_clicks', 'eff_orders', 'eff_revenue',
                'event_id', 'event__name',
            )
            .order_by('-eff_revenue')[:limit]
        )
        for row in qs:
            audience = row['audience_size'] or 0
            clicks = row['eff_clicks'] or 0
            rows.append({
                'channel': 'sms',
                'channel_label': 'SMS',
                'name': row['name'] or 'Untitled SMS',
                'send_time': row['send_time'].isoformat() if row['send_time'] else None,
                'revenue': row['eff_revenue'] or ZERO,
                'orders': row['eff_orders'] or 0,
                'sends': audience,
                'opens': None,
                'clicks': clicks,
                'open_rate': None,
                'click_rate': _safe_div(clicks, audience),
                'event_id': str(row['event_id']),
                'event_name': row['event__name'],
            })
        return rows

    def _engagement_trends(self):
        """Monthly opens and clicks across email + SMS for the engagement chart."""
        months = {}

        def bucket(key):
            return months.setdefault(key, {
                'month': key,
                'email_opens': 0,
                'email_clicks': 0,
                'sms_clicks': 0,
            })

        for row in (
            self._email_qs()
            .exclude(send_time__isnull=True)
            .annotate(
                m=TruncMonth('send_time'),
                eff_clicks=self._coalesce_int('manual_clicks', 'unique_clicks'),
            )
            .values('m')
            .annotate(
                opens=Coalesce(Sum('unique_opens'), 0),
                clicks=Coalesce(Sum('eff_clicks'), 0),
            )
            .order_by('m')
        ):
            month_key = row['m'].date().isoformat() if row['m'] else None
            if month_key:
                b = bucket(month_key)
                b['email_opens'] = row['opens']
                b['email_clicks'] = row['clicks']

        for row in (
            self._sms_qs()
            .exclude(send_time__isnull=True)
            .annotate(
                m=TruncMonth('send_time'),
                eff_clicks=self._coalesce_int('manual_clicks', 'unique_clicks'),
            )
            .values('m')
            .annotate(clicks=Coalesce(Sum('eff_clicks'), 0))
            .order_by('m')
        ):
            month_key = row['m'].date().isoformat() if row['m'] else None
            if month_key:
                bucket(month_key)['sms_clicks'] = row['clicks']

        return sorted(months.values(), key=lambda r: r['month'])

    def _top_events_by_roi(self, limit=10):
        """Per-event ROI using the isolated-Subquery pattern (CLAUDE.md).

        Confirmed-only: revenue and spend roll up from confirmed campaigns/expenses.
        Manual overrides take precedence via Coalesce.
        """
        event_qs = Event.objects.filter(
            organization=self.organization,
            deleted_at__isnull=True,
        )

        email_subq = EventEmailCampaign.objects.filter(
            event=OuterRef('pk'),
            deleted_at__isnull=True,
            confirmed_at__isnull=False,
        )
        if self.window_start is not None:
            email_subq = email_subq.filter(send_time__gte=self.window_start)
        email_revenue_sq = Subquery(
            email_subq.values('event').annotate(
                s=Sum(Coalesce(F('manual_revenue'), F('ecommerce_revenue'),
                               output_field=DecimalField(max_digits=12, decimal_places=2)))
            ).values('s')[:1],
            output_field=DecimalField(max_digits=12, decimal_places=2),
        )

        sms_subq = EventSMSCampaign.objects.filter(
            event=OuterRef('pk'),
            deleted_at__isnull=True,
            confirmed_at__isnull=False,
        )
        if self.window_start is not None:
            sms_subq = sms_subq.filter(send_time__gte=self.window_start)
        sms_revenue_sq = Subquery(
            sms_subq.values('event').annotate(
                s=Sum(Coalesce(F('manual_revenue'), F('revenue'),
                               output_field=DecimalField(max_digits=12, decimal_places=2)))
            ).values('s')[:1],
            output_field=DecimalField(max_digits=12, decimal_places=2),
        )

        ads_subq = EventExpense.objects.filter(
            event=OuterRef('pk'),
            deleted_at__isnull=True,
            source='meta_ads',
            confirmed_at__isnull=False,
        )
        if self.window_start is not None:
            ads_subq = ads_subq.filter(expense_date__gte=self.window_start.date())
        ads_spend_sq = Subquery(
            ads_subq.values('event').annotate(s=Sum('amount')).values('s')[:1],
            output_field=DecimalField(max_digits=12, decimal_places=2),
        )
        ads_revenue_sq = Subquery(
            ads_subq.values('event').annotate(s=Sum('manual_attributed_revenue')).values('s')[:1],
            output_field=DecimalField(max_digits=12, decimal_places=2),
        )

        annotated = event_qs.annotate(
            email_revenue=Coalesce(email_revenue_sq, ZERO, output_field=DecimalField(max_digits=12, decimal_places=2)),
            sms_revenue=Coalesce(sms_revenue_sq, ZERO, output_field=DecimalField(max_digits=12, decimal_places=2)),
            ads_spend=Coalesce(ads_spend_sq, ZERO, output_field=DecimalField(max_digits=12, decimal_places=2)),
            ads_revenue=Coalesce(ads_revenue_sq, ZERO, output_field=DecimalField(max_digits=12, decimal_places=2)),
        ).filter(
            Q(email_revenue__gt=0) | Q(sms_revenue__gt=0) | Q(ads_spend__gt=0) | Q(ads_revenue__gt=0)
        )

        rows = []
        for event in annotated.only('id', 'name', 'start_date')[:200]:
            email_rev = event.email_revenue or ZERO
            sms_rev = event.sms_revenue or ZERO
            spend = event.ads_spend or ZERO
            ads_rev = event.ads_revenue or ZERO
            attributed = email_rev + sms_rev + ads_rev
            net = attributed - spend
            roi = (net / spend) if spend else None
            rows.append({
                'event_id': str(event.id),
                'event_name': event.name,
                'event_date': event.start_date.isoformat() if event.start_date else None,
                'email_revenue': email_rev,
                'sms_revenue': sms_rev,
                'ads_revenue': ads_rev,
                'ads_spend': spend,
                'attributed_revenue': attributed,
                'roi': roi.quantize(Decimal('0.0001')) if roi is not None else None,
            })

        def sort_key(row):
            r = row['roi']
            return r if r is not None else Decimal('-9999')

        rows.sort(key=sort_key, reverse=True)
        return rows[:limit]
