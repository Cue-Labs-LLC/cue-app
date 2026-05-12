import logging
from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from tickets.models import (
    AIRecommendation,
    Event,
    EventEmailCampaign,
    EventExpense,
    ExternalSurveyResponse,
    SurveyInvitation,
    TICKETING_TYPE_DIRECT,
)
from tickets.services.churn_detection.churn_calculator import ChurnDetectionService
from tickets.services.forecasting.preview import generate_forecast_preview

logger = logging.getLogger(__name__)


UNRESOLVED_STATUSES = [
    AIRecommendation.Status.NEW,
    AIRecommendation.Status.REVIEWED,
]


class AIRecommendationGenerator:
    """Generate deterministic, reviewed recommendations for one organization."""

    def __init__(self, organization):
        self.organization = organization

    def run_all(self) -> list[AIRecommendation]:
        recommendations = []
        for detector in (
            self.detect_sales_pacing_risks,
            self.detect_post_event_wrapups,
            self.detect_winback_audience,
            self.detect_marketing_attribution_gaps,
        ):
            try:
                recommendations.extend(detector())
            except Exception:
                logger.exception(
                    "AI recommendation detector %s failed for org %s",
                    getattr(detector, '__name__', detector.__class__.__name__),
                    self.organization_id_for_log,
                )
        return recommendations

    @property
    def organization_id_for_log(self):
        return getattr(self.organization, 'id', None)

    def detect_sales_pacing_risks(self) -> list[AIRecommendation]:
        today = timezone.localdate()
        cutoff = today + timedelta(days=90)
        events = (
            Event.objects.filter(
                organization=self.organization,
                ticketing_type=TICKETING_TYPE_DIRECT,
                start_date__gte=today,
                start_date__lte=cutoff,
                deleted_at__isnull=True,
                capacity__isnull=False,
            )
            .select_related('venue')
            .order_by('start_date')[:50]
        )

        results = []
        for event in events:
            if not event.capacity or event.capacity <= 0:
                continue
            days_until_event = (event.start_date - today).days
            sold = int(event.cached_paid_ticket_count or 0)
            expected = self._expected_tickets_today(event, days_until_event)
            if expected is None:
                if days_until_event <= 14 and sold == 0:
                    expected = max(1, int(event.capacity * Decimal('0.05')))
                else:
                    continue

            expected = int(expected)
            if expected < 5 and not (days_until_event <= 14 and sold == 0):
                continue
            if sold >= int(expected * Decimal('0.60')):
                continue

            gap = max(expected - sold, 0)
            priority = AIRecommendation.Priority.HIGH if days_until_event <= 14 else AIRecommendation.Priority.MEDIUM
            results.append(self._upsert(
                dedupe_key=f'sales_pacing:{event.id}',
                kind=AIRecommendation.Kind.SALES_PACING,
                priority=priority,
                confidence=Decimal('0.760'),
                title=f'{event.name} is behind expected sales pace',
                summary=(
                    f'{event.name} has sold {sold} paid tickets with {days_until_event} days until the event. '
                    f'Historical pacing suggests roughly {expected} tickets by now, leaving a gap of {gap}.'
                ),
                evidence={
                    'event_id': str(event.id),
                    'event_date': event.start_date.isoformat(),
                    'days_until_event': days_until_event,
                    'paid_tickets_sold': sold,
                    'expected_tickets_today': expected,
                    'ticket_gap': gap,
                    'capacity': event.capacity,
                },
                action={
                    'type': 'forecast_review',
                    'label': 'Review forecast',
                    'url': reverse('tickets:forecast_tool'),
                    'payload': {'event_id': str(event.id)},
                },
                event=event,
            ))
        return results

    def detect_post_event_wrapups(self) -> list[AIRecommendation]:
        today = timezone.localdate()
        window_start = today - timedelta(days=45)
        events = (
            Event.objects.filter(
                organization=self.organization,
                start_date__gte=window_start,
                start_date__lt=today,
                deleted_at__isnull=True,
            )
            .select_related('venue')
            .order_by('-start_date')[:60]
        )

        results = []
        for event in events:
            missing = []
            if not EventExpense.objects.filter(event=event, deleted_at__isnull=True).exists():
                missing.append('expenses')
            has_invitation = SurveyInvitation.objects.filter(event=event).exists()
            has_external_response = ExternalSurveyResponse.objects.filter(event=event).exists()
            if not has_invitation and not has_external_response:
                missing.append('survey follow-up')
            if not EventEmailCampaign.objects.filter(event=event, deleted_at__isnull=True).exists():
                missing.append('email campaign attribution')
            if not missing:
                continue

            priority = AIRecommendation.Priority.HIGH if 'expenses' in missing else AIRecommendation.Priority.MEDIUM
            results.append(self._upsert(
                dedupe_key=f'post_event_wrapup:{event.id}',
                kind=AIRecommendation.Kind.POST_EVENT_WRAPUP,
                priority=priority,
                confidence=Decimal('0.820'),
                title=f'Finish post-event wrap-up for {event.name}',
                summary=(
                    f'{event.name} ended on {event.start_date:%b %d, %Y}. '
                    f'Review the event while the details are fresh: missing {", ".join(missing)}.'
                ),
                evidence={
                    'event_id': str(event.id),
                    'event_date': event.start_date.isoformat(),
                    'missing_items': missing,
                },
                action={
                    'type': 'event_summary',
                    'label': 'Open event review',
                    'url': reverse('tickets:event_detail', args=[event.id]),
                    'payload': {'event_id': str(event.id)},
                },
                event=event,
            ))
        return results

    def detect_winback_audience(self) -> list[AIRecommendation]:
        result = ChurnDetectionService(self.organization).calculate(days_threshold=90)
        stats = result['stats']
        total_count = int(stats['total_count'] or 0)
        if total_count == 0:
            return []

        top_customer = (
            result['customers']
            .order_by('-lifetime_value', 'name')
            .first()
        )
        total_ltv = stats['total_ltv_at_risk'] or Decimal('0.00')
        priority = AIRecommendation.Priority.HIGH if total_ltv >= Decimal('1000.00') or total_count >= 10 else AIRecommendation.Priority.MEDIUM
        recommendation = self._upsert(
            dedupe_key='winback_audience:90',
            kind=AIRecommendation.Kind.WINBACK_AUDIENCE,
            priority=priority,
            confidence=Decimal('0.790'),
            title='High-value customers are ready for a win-back review',
            summary=(
                f'{total_count} repeat customers have been inactive for more than 90 days. '
                f'Their combined LTV is ${total_ltv:,.2f}; review them before the audience goes colder.'
            ),
            evidence={
                'days_threshold': 90,
                'customer_count': total_count,
                'total_ltv_at_risk': str(total_ltv),
                'avg_ltv': str(stats['avg_ltv']),
                'segment_breakdown': stats['segment_breakdown'],
                'top_customer_id': str(top_customer.id) if top_customer else None,
            },
            action={
                'type': 'churn_review',
                'label': 'Review win-back list',
                'url': f"{reverse('tickets:churn_overview')}?days=90",
                'payload': {'days_threshold': 90},
            },
            customer=top_customer,
        )
        return [recommendation]

    def detect_marketing_attribution_gaps(self) -> list[AIRecommendation]:
        org = self.organization
        if not (org.meta_ads_account_id or (org.mailchimp_access_token and org.mailchimp_dc)):
            return []

        today = timezone.localdate()
        events = (
            Event.objects.filter(
                organization=org,
                start_date__gte=today - timedelta(days=90),
                start_date__lte=today + timedelta(days=14),
                deleted_at__isnull=True,
            )
            .select_related('venue')
            .order_by('-start_date')[:80]
        )

        results = []
        for event in events:
            missing = []
            action_url = reverse('tickets:event_detail', args=[event.id])
            action_type = 'open_url'
            if org.meta_ads_account_id and not EventExpense.objects.filter(
                event=event,
                source='meta_ads',
                deleted_at__isnull=True,
            ).exists():
                missing.append('Meta Ads spend')
                action_url = reverse('tickets:event_meta_ads_match', args=[event.id])
                action_type = 'campaign_match'
            if org.mailchimp_access_token and org.mailchimp_dc and not EventEmailCampaign.objects.filter(
                event=event,
                source='mailchimp',
                deleted_at__isnull=True,
            ).exists():
                missing.append('Mailchimp campaign report')
                if action_type == 'open_url':
                    action_url = reverse('tickets:event_mailchimp_match', args=[event.id])
                    action_type = 'campaign_match'
            if not missing:
                continue

            results.append(self._upsert(
                dedupe_key=f'marketing_attribution:{event.id}',
                kind=AIRecommendation.Kind.MARKETING_ATTRIBUTION,
                priority=AIRecommendation.Priority.MEDIUM,
                confidence=Decimal('0.700'),
                title=f'Link marketing attribution for {event.name}',
                summary=(
                    f'{event.name} has connected marketing channels, but Cue is missing '
                    f'{", ".join(missing)}. Link the campaign data before reviewing profitability.'
                ),
                evidence={
                    'event_id': str(event.id),
                    'event_date': event.start_date.isoformat(),
                    'missing_items': missing,
                    'meta_connected': bool(org.meta_ads_account_id),
                    'mailchimp_connected': bool(org.mailchimp_access_token and org.mailchimp_dc),
                },
                action={
                    'type': action_type,
                    'label': 'Link campaigns',
                    'url': action_url,
                    'payload': {'event_id': str(event.id)},
                },
                event=event,
            ))
        return results

    def _expected_tickets_today(self, event, days_until_event):
        try:
            preview = generate_forecast_preview(
                venue_id=str(event.venue_id),
                event_date=event.start_date,
                capacity=event.capacity,
                min_events=1,
                organization=self.organization,
            )
        except Exception:
            logger.exception("Forecast preview failed for AI recommendation event %s", event.id)
            return None

        if not preview.get('has_sufficient_data'):
            return None
        for point in preview.get('curve_points', []):
            if int(point.get('days_before', -999)) == days_until_event:
                return int(point.get('expected_tickets') or 0)
        return None

    def _upsert(
        self,
        *,
        dedupe_key,
        kind,
        priority,
        confidence,
        title,
        summary,
        evidence,
        action,
        event=None,
        customer=None,
    ):
        existing = AIRecommendation.objects.filter(
            organization=self.organization,
            dedupe_key=dedupe_key,
        ).first()
        if existing and existing.status in {
            AIRecommendation.Status.DISMISSED,
            AIRecommendation.Status.RESOLVED,
        }:
            return existing

        defaults = {
            'kind': kind,
            'priority': priority,
            'confidence': confidence,
            'title': title,
            'summary': summary,
            'evidence_json': evidence,
            'recommended_action_json': action,
            'event': event,
            'customer': customer,
        }
        recommendation, _created = AIRecommendation.objects.update_or_create(
            organization=self.organization,
            dedupe_key=dedupe_key,
            defaults=defaults,
        )
        return recommendation
