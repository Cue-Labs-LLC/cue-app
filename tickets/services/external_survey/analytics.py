"""
Analytics over ExternalSurveyResponse data.
"""
import logging
import uuid
from django.db.models import Count, Q
from django.db.models.functions import TruncMonth

logger = logging.getLogger(__name__)


NO_MARKET_VALUE = 'no-market'


class ExternalSurveyAnalytics:
    def __init__(self, organization):
        self.organization = organization

    def calculate(self, market=None, city=None):
        from tickets.models import ExternalSurveyResponse

        qs = ExternalSurveyResponse.objects.filter(organization=self.organization)
        if market == NO_MARKET_VALUE:
            qs = qs.filter(event__market__isnull=True)
        elif market:
            try:
                uuid.UUID(str(market))
            except (TypeError, ValueError):
                qs = qs.none()
            else:
                qs = qs.filter(event__market_id=market)
        elif city:
            qs = qs.filter(Q(event__market__name=city) | Q(event__market__geography_value=city))

        total = qs.count()

        # NPS — single aggregate
        nps_qs = qs.filter(nps_score__isnull=False)
        nps_agg = nps_qs.aggregate(
            nps_total=Count('id'),
            promoters=Count('id', filter=Q(nps_score__gte=9)),
            passives=Count('id', filter=Q(nps_score__gte=7, nps_score__lte=8)),
            detractors=Count('id', filter=Q(nps_score__lte=6)),
        )
        nps_total = nps_agg['nps_total']
        promoters = nps_agg['promoters']
        passives = nps_agg['passives']
        detractors = nps_agg['detractors']

        if nps_total > 0:
            nps_score = round((promoters - detractors) / nps_total * 100)
            promoters_pct = round(promoters / nps_total * 100)
            passives_pct = round(passives / nps_total * 100)
            detractors_pct = round(detractors / nps_total * 100)
        else:
            nps_score = None
            promoters_pct = passives_pct = detractors_pct = 0

        # NPS over time — monthly series scoped to current filter
        monthly_rows = (
            nps_qs
            .annotate(month=TruncMonth('responded_at'))
            .values('month')
            .annotate(
                n=Count('id'),
                promoters=Count('id', filter=Q(nps_score__gte=9)),
                detractors=Count('id', filter=Q(nps_score__lte=6)),
            )
            .order_by('month')
        )
        dated_rows = [row for row in monthly_rows if row['month'] is not None]
        nps_over_time = [
            {
                'month': row['month'].strftime('%Y-%m'),
                'label': row['month'].strftime('%b %Y'),
                'n': row['n'],
                'nps_score': round((row['promoters'] - row['detractors']) / row['n'] * 100) if row['n'] else None,
            }
            for row in dated_rows
        ]

        # Cumulative NPS over time — running all-time NPS through each month
        cum_n = cum_promoters = cum_detractors = 0
        nps_cumulative = []
        for row in dated_rows:
            cum_n += row['n']
            cum_promoters += row['promoters']
            cum_detractors += row['detractors']
            nps_cumulative.append({
                'month': row['month'].strftime('%Y-%m'),
                'label': row['month'].strftime('%b %Y'),
                'n': cum_n,
                'nps_score': round((cum_promoters - cum_detractors) / cum_n * 100) if cum_n else None,
            })

        # Rating breakdown
        rating_breakdown = list(
            qs.exclude(overall_rating='')
            .values('overall_rating')
            .annotate(count=Count('id'))
            .order_by('-count')
        )

        # Market NPS breakdown — one query
        city_rows = ExternalSurveyResponse.objects.filter(organization=self.organization).filter(
            event__isnull=False,
        ).values('event__market_id', 'event__market__name').annotate(
            total=Count('id'),
            nps_n=Count('id', filter=Q(nps_score__isnull=False)),
            promoters=Count('id', filter=Q(nps_score__gte=9)),
            passives=Count('id', filter=Q(nps_score__gte=7, nps_score__lte=8)),
            detractors=Count('id', filter=Q(nps_score__lte=6)),
        ).order_by('-total')

        city_breakdown = []
        for row in city_rows:
            market_id = row['event__market_id']
            market_name = row['event__market__name'] or 'No market'
            n = row['nps_n']
            score = None
            if n > 0:
                score = round((row['promoters'] - row['detractors']) / n * 100)
            city_breakdown.append({
                'market_id': str(market_id) if market_id else '',
                'market_name': market_name,
                'market_label': market_name,
                'city': market_name,
                'city_raw': str(market_id) if market_id else NO_MARKET_VALUE,
                'total': row['total'],
                'nps_n': n,
                'nps_score': score,
                'promoters': row['promoters'],
                'passives': row['passives'],
                'detractors': row['detractors'],
            })

        return {
            'total': total,
            'nps_total': nps_total,
            'nps_score': nps_score,
            'promoters': promoters,
            'passives': passives,
            'detractors': detractors,
            'promoters_pct': promoters_pct,
            'passives_pct': passives_pct,
            'detractors_pct': detractors_pct,
            'rating_breakdown': rating_breakdown,
            'city_breakdown': city_breakdown,
            'nps_over_time': nps_over_time,
            'nps_cumulative': nps_cumulative,
        }
