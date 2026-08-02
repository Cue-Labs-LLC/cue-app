"""
Aggregate analytics across ALL survey responses for an organization —
both Cue-native surveys (SurveyResponse / SurveyAnswer) and imported
external surveys (ExternalSurveyResponse).

This mirrors the per-event merge already done in views._compute_event_stats:
promoter / passive / detractor counts are summed across the two sources and a
single combined NPS is computed with the shared formula
    nps_score = round((promoters - detractors) / nps_total * 100)
Internal NPS lives in SurveyAnswer.nps_score (question_type 'nps'); external in
ExternalSurveyResponse.nps_score. Both link to an event -> market and carry a
response timestamp, so the market / event-date filters apply to both.

Rating breakdown stays external-only: external `overall_rating` is free text
while internal ratings are a 1-5 `star_rating` — different scales. The internal
rating is surfaced separately as `avg_star_rating`.
"""
import logging
import uuid

from django.db.models import Avg, Count, Q
from django.db.models.functions import TruncMonth

from ..external_survey.analytics import NO_MARKET_VALUE

logger = logging.getLogger(__name__)

# Shared NPS bucket predicates (promoters 9-10, passives 7-8, detractors 0-6).
_NPS_AGG = dict(
    nps_total=Count('id'),
    promoters=Count('id', filter=Q(nps_score__gte=9)),
    passives=Count('id', filter=Q(nps_score__gte=7, nps_score__lte=8)),
    detractors=Count('id', filter=Q(nps_score__lte=6)),
)


def _nps_score(promoters, detractors, nps_total):
    """The one NPS formula used everywhere. None when there's no NPS data."""
    if nps_total > 0:
        return round((promoters - detractors) / nps_total * 100)
    return None


def _empty_raw():
    return {
        'total': 0,
        'nps_total': 0, 'promoters': 0, 'passives': 0, 'detractors': 0,
        'monthly': {},   # 'YYYY-MM' -> {label, n, promoters, detractors}
        'markets': {},   # market_key -> {market_id, market_name, total, nps_n, promoters, passives, detractors}
    }


class SurveyAnalytics:
    def __init__(self, organization):
        self.organization = organization

    # -- public -----------------------------------------------------------

    def calculate(self, market=None, city=None, event_from=None, event_to=None):
        internal = self._internal_raw(market, city, event_from, event_to)
        external = self._external_raw(market, city, event_from, event_to)
        return self._merge_and_format(internal, external)

    # -- filter helpers ---------------------------------------------------

    def _apply_filters(self, qs, prefix, market, city, event_from, event_to,
                       include_market=True):
        """Apply market / city / event-date filters. `prefix` reaches the Event
        from the current model, e.g. '' for ExternalSurveyResponse (has .event)
        or 'response__' for SurveyAnswer / SurveyResponse chained via .response.

        `include_market=False` applies only the date window — used for the market
        breakdown, which stays comparable across all markets even when one market
        is selected (matching ExternalSurveyAnalytics' city_rows)."""
        m = f'{prefix}event__market'
        d = f'{prefix}event__start_date'
        if include_market:
            if market == NO_MARKET_VALUE:
                qs = qs.filter(**{f'{m}__isnull': True})
            elif market:
                try:
                    uuid.UUID(str(market))
                except (TypeError, ValueError):
                    return qs.none()
                qs = qs.filter(**{f'{m}_id': market})
            elif city:
                qs = qs.filter(
                    Q(**{f'{m}__name': city}) | Q(**{f'{m}__geography_value': city})
                )
        if event_from:
            qs = qs.filter(**{f'{d}__gte': event_from})
        if event_to:
            qs = qs.filter(**{f'{d}__lte': event_to})
        return qs

    # -- external half ----------------------------------------------------

    def _external_raw(self, market, city, event_from, event_to):
        from tickets.models import ExternalSurveyResponse

        raw = _empty_raw()
        qs = ExternalSurveyResponse.objects.filter(organization=self.organization)
        qs = self._apply_filters(qs, '', market, city, event_from, event_to)

        raw['total'] = qs.count()

        nps_qs = qs.filter(nps_score__isnull=False)
        agg = nps_qs.aggregate(**_NPS_AGG)
        raw.update(nps_total=agg['nps_total'], promoters=agg['promoters'],
                   passives=agg['passives'], detractors=agg['detractors'])

        # Bucket the time series by event date (not response date) so the trend
        # reflects when events happened. Responses with no linked event have no
        # event date and drop out of the series (handled in _monthly).
        raw['monthly'] = self._monthly(nps_qs, 'event__start_date')
        # Market breakdown ignores the market/city filter (date window only) so
        # it stays a comparison across every market.
        market_qs = self._apply_filters(
            ExternalSurveyResponse.objects.filter(organization=self.organization),
            '', market, city, event_from, event_to, include_market=False,
        )
        raw['markets'] = self._markets(
            market_qs, market_qs.filter(nps_score__isnull=False),
            market_id='event__market_id', market_name='event__market__name',
        )
        # Rating breakdown — external only (free-text overall_rating).
        raw['rating_breakdown'] = list(
            qs.exclude(overall_rating='')
            .values('overall_rating')
            .annotate(count=Count('id'))
            .order_by('-count')
        )
        return raw

    # -- internal half ----------------------------------------------------

    def _internal_raw(self, market, city, event_from, event_to):
        from tickets.models import SurveyResponse, SurveyAnswer

        raw = _empty_raw()

        # Total = responses (one row per completed survey).
        resp_qs = SurveyResponse.objects.filter(organization=self.organization)
        resp_qs = self._apply_filters(resp_qs, '', market, city, event_from, event_to)
        raw['total'] = resp_qs.count()

        # NPS / star buckets = answers (chained through .response).
        ans_qs = SurveyAnswer.objects.filter(response__organization=self.organization)
        ans_qs = self._apply_filters(ans_qs, 'response__', market, city, event_from, event_to)

        nps_qs = ans_qs.filter(nps_score__isnull=False)
        agg = nps_qs.aggregate(**_NPS_AGG)
        raw.update(nps_total=agg['nps_total'], promoters=agg['promoters'],
                   passives=agg['passives'], detractors=agg['detractors'])

        raw['avg_star_rating'] = ans_qs.filter(star_rating__isnull=False).aggregate(
            avg=Avg('star_rating'))['avg']

        # Bucket the time series by event date (not submission date) — see the
        # external half for rationale. Reaches the event via .response.
        raw['monthly'] = self._monthly(nps_qs, 'response__event__start_date')
        # Market breakdown ignores the market/city filter (date window only).
        # Per-market response totals come from responses; NPS buckets from answers.
        resp_market_qs = self._apply_filters(
            SurveyResponse.objects.filter(organization=self.organization),
            '', market, city, event_from, event_to, include_market=False,
        )
        ans_market_qs = self._apply_filters(
            SurveyAnswer.objects.filter(
                response__organization=self.organization, nps_score__isnull=False),
            'response__', market, city, event_from, event_to, include_market=False,
        )
        raw['markets'] = self._markets(
            resp_market_qs, ans_market_qs,
            market_id='event__market_id', market_name='event__market__name',
            nps_market_id='response__event__market_id',
            nps_market_name='response__event__market__name',
        )
        return raw

    # -- shared aggregation shapes ---------------------------------------

    def _monthly(self, nps_qs, date_field):
        rows = (
            nps_qs.annotate(month=TruncMonth(date_field))
            .values('month')
            .annotate(
                n=Count('id'),
                promoters=Count('id', filter=Q(nps_score__gte=9)),
                detractors=Count('id', filter=Q(nps_score__lte=6)),
            )
            .order_by('month')
        )
        out = {}
        for row in rows:
            if row['month'] is None:
                continue
            out[row['month'].strftime('%Y-%m')] = {
                'label': row['month'].strftime('%b %Y'),
                'n': row['n'],
                'promoters': row['promoters'],
                'detractors': row['detractors'],
            }
        return out

    def _markets(self, total_qs, nps_qs, market_id, market_name,
                 nps_market_id=None, nps_market_name=None):
        """Per-market raw counts keyed by market id (NO_MARKET_VALUE when null).
        `total_qs` supplies response totals; `nps_qs` supplies NPS buckets. They
        may be different models (internal), so the market field paths can differ."""
        nps_market_id = nps_market_id or market_id
        nps_market_name = nps_market_name or market_name
        # Group by event presence (a response can have an event but no market ->
        # "No market"), mirroring ExternalSurveyAnalytics' event__isnull=False.
        total_event = market_id[:-len('__market_id')]
        nps_event = nps_market_id[:-len('__market_id')]

        markets = {}

        def _key(mid):
            return str(mid) if mid else NO_MARKET_VALUE

        total_rows = (
            total_qs.filter(**{f'{total_event}__isnull': False})
            .values(market_id, market_name)
            .annotate(total=Count('id'))
        )
        for row in total_rows:
            key = _key(row[market_id])
            markets[key] = {
                'market_id': str(row[market_id]) if row[market_id] else '',
                'market_name': row[market_name] or 'No market',
                'total': row['total'], 'nps_n': 0,
                'promoters': 0, 'passives': 0, 'detractors': 0,
            }

        nps_rows = (
            nps_qs.filter(**{f'{nps_event}__isnull': False})
            .values(nps_market_id, nps_market_name)
            .annotate(
                nps_n=Count('id'),
                promoters=Count('id', filter=Q(nps_score__gte=9)),
                passives=Count('id', filter=Q(nps_score__gte=7, nps_score__lte=8)),
                detractors=Count('id', filter=Q(nps_score__lte=6)),
            )
        )
        for row in nps_rows:
            key = _key(row[nps_market_id])
            bucket = markets.setdefault(key, {
                'market_id': str(row[nps_market_id]) if row[nps_market_id] else '',
                'market_name': row[nps_market_name] or 'No market',
                'total': 0, 'nps_n': 0,
                'promoters': 0, 'passives': 0, 'detractors': 0,
            })
            bucket['nps_n'] += row['nps_n']
            bucket['promoters'] += row['promoters']
            bucket['passives'] += row['passives']
            bucket['detractors'] += row['detractors']
        return markets

    # -- merge + format ---------------------------------------------------

    def _merge_and_format(self, internal, external):
        total = internal['total'] + external['total']
        nps_total = internal['nps_total'] + external['nps_total']
        promoters = internal['promoters'] + external['promoters']
        passives = internal['passives'] + external['passives']
        detractors = internal['detractors'] + external['detractors']

        nps_score = _nps_score(promoters, detractors, nps_total)
        if nps_total > 0:
            promoters_pct = round(promoters / nps_total * 100)
            passives_pct = round(passives / nps_total * 100)
            detractors_pct = round(detractors / nps_total * 100)
        else:
            promoters_pct = passives_pct = detractors_pct = 0

        # Monthly — merge raw counts by month, then compute score + cumulative.
        months = {}
        for src in (internal['monthly'], external['monthly']):
            for key, row in src.items():
                bucket = months.setdefault(
                    key, {'label': row['label'], 'n': 0, 'promoters': 0, 'detractors': 0})
                bucket['n'] += row['n']
                bucket['promoters'] += row['promoters']
                bucket['detractors'] += row['detractors']
        ordered = sorted(months.items())  # by 'YYYY-MM'
        nps_over_time = [
            {'month': key, 'label': row['label'], 'n': row['n'],
             'nps_score': _nps_score(row['promoters'], row['detractors'], row['n'])}
            for key, row in ordered
        ]
        cum_n = cum_p = cum_d = 0
        nps_cumulative = []
        for key, row in ordered:
            cum_n += row['n']
            cum_p += row['promoters']
            cum_d += row['detractors']
            nps_cumulative.append({
                'month': key, 'label': row['label'], 'n': cum_n,
                'nps_score': _nps_score(cum_p, cum_d, cum_n),
            })

        # Markets — merge raw counts by market, then compute per-market score.
        markets = {}
        for src in (internal['markets'], external['markets']):
            for key, row in src.items():
                bucket = markets.setdefault(key, {
                    'market_id': row['market_id'], 'market_name': row['market_name'],
                    'total': 0, 'nps_n': 0, 'promoters': 0, 'passives': 0, 'detractors': 0})
                if not bucket['market_id']:
                    bucket['market_id'] = row['market_id']
                if bucket['market_name'] in ('', 'No market') and row['market_name']:
                    bucket['market_name'] = row['market_name']
                for f in ('total', 'nps_n', 'promoters', 'passives', 'detractors'):
                    bucket[f] += row[f]
        city_breakdown = []
        for key, row in markets.items():
            name = row['market_name'] or 'No market'
            city_breakdown.append({
                'market_id': row['market_id'],
                'market_name': name, 'market_label': name, 'city': name,
                'city_raw': row['market_id'] or NO_MARKET_VALUE,
                'total': row['total'], 'nps_n': row['nps_n'],
                'nps_score': _nps_score(row['promoters'], row['detractors'], row['nps_n']),
                'promoters': row['promoters'], 'passives': row['passives'],
                'detractors': row['detractors'],
            })
        city_breakdown.sort(key=lambda r: r['total'], reverse=True)

        return {
            'total': total,
            'internal_total': internal['total'],
            'external_total': external['total'],
            'nps_total': nps_total,
            'nps_score': nps_score,
            'promoters': promoters, 'passives': passives, 'detractors': detractors,
            'promoters_pct': promoters_pct, 'passives_pct': passives_pct,
            'detractors_pct': detractors_pct,
            'avg_star_rating': (round(internal['avg_star_rating'], 1)
                                if internal.get('avg_star_rating') else None),
            'rating_breakdown': external.get('rating_breakdown', []),
            'city_breakdown': city_breakdown,
            'nps_over_time': nps_over_time,
            'nps_cumulative': nps_cumulative,
        }
