"""
Analytics over ExternalSurveyResponse data.
"""
import logging
from django.db.models import Count, Q

logger = logging.getLogger(__name__)


class ExternalSurveyAnalytics:
    def __init__(self, organization):
        self.organization = organization

    def calculate(self, upload_id=None, city=None):
        from tickets.models import ExternalSurveyResponse

        qs = ExternalSurveyResponse.objects.filter(organization=self.organization)
        if upload_id:
            qs = qs.filter(upload_id=upload_id)
        if city:
            if city == '__blank__':
                qs = qs.filter(city='')
            else:
                qs = qs.filter(city=city)

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

        # Rating breakdown
        rating_breakdown = list(
            qs.exclude(overall_rating='')
            .values('overall_rating')
            .annotate(count=Count('id'))
            .order_by('-count')
        )

        # City NPS breakdown — one query
        city_stats_raw = (
            ExternalSurveyResponse.objects.filter(organization=self.organization)
            .filter(upload_id=upload_id) if upload_id else
            ExternalSurveyResponse.objects.filter(organization=self.organization)
        )
        if city:
            city_stats_raw = city_stats_raw  # already filtered above; city breakdown still uses unfiltered for full picture

        city_stats_qs = (
            ExternalSurveyResponse.objects.filter(organization=self.organization)
        )
        if upload_id:
            city_stats_qs = city_stats_qs.filter(upload_id=upload_id)

        city_rows = city_stats_qs.values('city').annotate(
            total=Count('id'),
            nps_n=Count('id', filter=Q(nps_score__isnull=False)),
            promoters=Count('id', filter=Q(nps_score__gte=9)),
            passives=Count('id', filter=Q(nps_score__gte=7, nps_score__lte=8)),
            detractors=Count('id', filter=Q(nps_score__lte=6)),
        ).order_by('-total')

        city_breakdown = []
        for row in city_rows:
            n = row['nps_n']
            score = None
            if n > 0:
                score = round((row['promoters'] - row['detractors']) / n * 100)
            city_breakdown.append({
                'city': row['city'] or '(no city)',
                'city_raw': row['city'],
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
        }
