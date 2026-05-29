"""
Analytics over ExternalSurveyResponse data.
"""
import logging
from django.db.models import Count, Q
from django.db.models.functions import TruncMonth

logger = logging.getLogger(__name__)


class ExternalSurveyAnalytics:
    def __init__(self, organization):
        self.organization = organization

    def calculate(self, city=None):
        from tickets.models import ExternalSurveyResponse

        qs = ExternalSurveyResponse.objects.filter(organization=self.organization)
        if city:
            qs = qs.filter(event__venue__city=city)

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
        nps_over_time = [
            {
                'month': row['month'].strftime('%Y-%m'),
                'label': row['month'].strftime('%b %Y'),
                'n': row['n'],
                'nps_score': round((row['promoters'] - row['detractors']) / row['n'] * 100) if row['n'] else None,
            }
            for row in monthly_rows if row['month'] is not None
        ]

        # Rating breakdown
        rating_breakdown = list(
            qs.exclude(overall_rating='')
            .values('overall_rating')
            .annotate(count=Count('id'))
            .order_by('-count')
        )

        # City NPS breakdown — one query
        city_rows = ExternalSurveyResponse.objects.filter(organization=self.organization).filter(
            event__isnull=False,
            event__venue__isnull=False,
        ).exclude(
            event__venue__city=''
        ).values('event__venue__city').annotate(
            total=Count('id'),
            nps_n=Count('id', filter=Q(nps_score__isnull=False)),
            promoters=Count('id', filter=Q(nps_score__gte=9)),
            passives=Count('id', filter=Q(nps_score__gte=7, nps_score__lte=8)),
            detractors=Count('id', filter=Q(nps_score__lte=6)),
        ).order_by('-total')

        city_breakdown = []
        for row in city_rows:
            city_val = row['event__venue__city']
            n = row['nps_n']
            score = None
            if n > 0:
                score = round((row['promoters'] - row['detractors']) / n * 100)
            city_breakdown.append({
                'city': city_val,
                'city_raw': city_val,
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
        }
