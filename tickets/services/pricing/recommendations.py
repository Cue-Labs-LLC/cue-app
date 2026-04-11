"""
Smart pricing recommendations for direct-ticketing events.

The recommendation engine is deterministic and organization-scoped.
It suggests ticket tiers from historical paid-ticket pricing and
sell-through behavior. An optional AI rationale can be generated as
an explanation layer, but it never changes the numeric output.
"""

from __future__ import annotations

import logging
import math
import sys
from decimal import Decimal, ROUND_HALF_UP
from statistics import median
from typing import Iterable

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from tickets.models import Event, Ticket
from tickets.services.forecasting.historical_aggregator import HistoricalAggregator

logger = logging.getLogger(__name__)


class SmartPricingRecommender:
    """Recommend prices and tiers for a direct-ticketing event."""

    TIER_NAME_MAP = {
        2: ['Early Bird', 'General Admission'],
        3: ['Early Bird', 'Advance', 'General Admission'],
        4: ['Early Bird', 'Advance', 'General Admission', 'Final Release'],
    }

    QUANTILES = {
        2: [0.30, 0.65],
        3: [0.20, 0.50, 0.80],
        4: [0.15, 0.40, 0.65, 0.90],
    }

    MULTIPLIERS = [Decimal('0.90'), Decimal('0.95'), Decimal('1.00'), Decimal('1.05'), Decimal('1.10')]

    def __init__(self, organization, min_events: int = 1):
        self.organization = organization
        self.min_events = min_events
        self.aggregator = HistoricalAggregator(min_events=min_events, organization=organization)

    def recommend(self, event: Event) -> dict:
        """Return a structured pricing recommendation for an event."""
        blocking_reason = self._get_blocking_reason(event)
        if blocking_reason:
            return self._blocked_payload(event, blocking_reason)

        today = timezone.now().date()
        days_until_event = max(0, (event.start_date - today).days)
        reference = days_until_event if days_until_event > 0 else None

        baseline = self.aggregator.get_baseline_curve(
            venue=event.venue,
            reference_days_before=reference,
        )
        comparable_events = list(
            self.aggregator.get_events_used_for_baseline(
                venue=event.venue,
                reference_days_before=reference,
            )
            .exclude(id=event.id)
            .select_related('venue')
        )
        comparable_ids = [evt.id for evt in comparable_events]

        if not comparable_ids:
            return self._blocked_payload(event, 'Not enough historical events with paid ticket data yet.')

        paid_ticket_rows = list(
            Ticket.objects.filter(
                ticket_order__event_id__in=comparable_ids,
                ticket_order__refunded_at__isnull=True,
                price__gt=0,
            )
            .order_by('ticket_order__event_id', 'price')
            .values_list('ticket_order__event_id', 'price')
        )
        if not paid_ticket_rows:
            return self._blocked_payload(event, 'Not enough historical paid ticket data to recommend pricing.')

        prices_by_event = {}
        all_prices = []
        for event_id, price in paid_ticket_rows:
            price = Decimal(price).quantize(Decimal('0.01'))
            prices_by_event.setdefault(event_id, []).append(price)
            all_prices.append(price)

        viable_events = [evt for evt in comparable_events if prices_by_event.get(evt.id)]
        if not viable_events or not all_prices:
            return self._blocked_payload(event, 'Not enough historical paid ticket data to recommend pricing.')

        tier_count = self._choose_tier_count(prices_by_event.values())
        base_ladder = self._build_base_ladder(all_prices, tier_count)
        baseline_tickets, baseline_sell_through = self._baseline_ticket_expectation(viable_events, event.capacity)
        candidate = self._choose_best_ladder(base_ladder, baseline_tickets, baseline_sell_through, event.capacity)
        allotments = self._build_allotments(
            baseline=baseline,
            tier_count=tier_count,
            capacity=event.capacity,
            projected_tickets=candidate['projected_tickets'],
        )
        tier_names = self.TIER_NAME_MAP[tier_count]
        recommended_tiers = []
        for idx, (name, price, allotment) in enumerate(zip(tier_names, candidate['prices'], allotments)):
            recommended_tiers.append({
                'name': name,
                'price': self._money_str(price),
                'price_decimal': price,
                'allotment': allotment,
                'order': idx,
            })

        comparable_event_rows = [self._serialize_comparable_event(evt, prices_by_event[evt.id]) for evt in viable_events[:8]]
        confidence = self._confidence_label(len(viable_events), baseline.segment_type)
        projected_revenue = sum(
            tier['price_decimal'] * Decimal(tier['allotment'])
            for tier in recommended_tiers
        ).quantize(Decimal('0.01'))

        recommendation = {
            'ready': True,
            'blocking_reason': '',
            'can_apply': not event.saleable_ticket_types.exists(),
            'baseline_info': {
                'segment_type': baseline.segment_type,
                'segment_name': self._segment_name(baseline, event),
                'events_count': len(viable_events),
                'confidence': confidence,
            },
            'recommended_ticket_type': {
                'name': 'General Admission',
                'quantity_limit': event.capacity,
                'price': recommended_tiers[-1]['price'],
            },
            'recommended_tiers': [
                {
                    'name': tier['name'],
                    'price': tier['price'],
                    'allotment': tier['allotment'],
                    'order': tier['order'],
                }
                for tier in recommended_tiers
            ],
            'expected_outcome': {
                'projected_paid_tickets': candidate['projected_tickets'],
                'projected_sell_through_pct': round(candidate['projected_sell_through_pct'], 1),
                'projected_gross_revenue': self._money_str(projected_revenue),
                'historical_baseline_tickets': baseline_tickets,
            },
            'comparable_events': comparable_event_rows,
            'ai_rationale': self._generate_ai_rationale(
                event=event,
                tier_count=tier_count,
                confidence=confidence,
                comparable_events=comparable_event_rows,
                expected_outcome={
                    'projected_paid_tickets': candidate['projected_tickets'],
                    'projected_sell_through_pct': round(candidate['projected_sell_through_pct'], 1),
                    'projected_gross_revenue': self._money_str(projected_revenue),
                },
                recommended_tiers=recommended_tiers,
                baseline_info={
                    'segment_name': self._segment_name(baseline, event),
                    'events_count': len(viable_events),
                },
            ),
        }
        return recommendation

    def _get_blocking_reason(self, event: Event) -> str:
        if event.ticketing_type != 'direct':
            return 'Smart Pricing Recommendations are available for direct-ticketing events only.'
        if not event.venue_id:
            return 'Select a venue before generating a recommendation.'
        if not event.capacity or event.capacity <= 0:
            return 'Set an event capacity before generating a recommendation.'
        return ''

    def _blocked_payload(self, event: Event, reason: str) -> dict:
        return {
            'ready': False,
            'blocking_reason': reason,
            'can_apply': False,
            'baseline_info': {
                'segment_type': '',
                'segment_name': '',
                'events_count': 0,
                'confidence': 'low',
            },
            'recommended_ticket_type': {
                'name': 'General Admission',
                'quantity_limit': event.capacity,
                'price': '0.00',
            },
            'recommended_tiers': [],
            'expected_outcome': {
                'projected_paid_tickets': 0,
                'projected_sell_through_pct': 0.0,
                'projected_gross_revenue': '0.00',
                'historical_baseline_tickets': 0,
            },
            'comparable_events': [],
            'ai_rationale': '',
        }

    def _choose_tier_count(self, price_sets: Iterable[list[Decimal]]) -> int:
        distinct_counts = []
        for prices in price_sets:
            distinct_counts.append(len({price for price in prices}))
        tierish_counts = [count for count in distinct_counts if 2 <= count <= 4]
        if not tierish_counts:
            return 3
        return max(2, min(4, int(round(median(tierish_counts)))))

    def _build_base_ladder(self, all_prices: list[Decimal], tier_count: int) -> list[Decimal]:
        sorted_prices = sorted(all_prices)
        ladder = [self._percentile(sorted_prices, q) for q in self.QUANTILES[tier_count]]
        return self._ensure_monotonic_prices(ladder)

    def _baseline_ticket_expectation(self, comparable_events: list[Event], target_capacity: int) -> tuple[int, float]:
        sell_through_values = []
        paid_tickets = []
        for event in comparable_events:
            tickets = int(event.cached_paid_ticket_count or 0)
            if tickets <= 0:
                continue
            paid_tickets.append(tickets)
            if event.capacity and event.capacity > 0:
                sell_through_values.append(min(1.0, tickets / float(event.capacity)))

        if sell_through_values:
            sell_through = sum(sell_through_values) / len(sell_through_values)
            baseline_tickets = min(target_capacity, int(round(target_capacity * sell_through)))
        elif paid_tickets:
            baseline_tickets = min(target_capacity, int(round(sum(paid_tickets) / len(paid_tickets))))
            sell_through = baseline_tickets / float(target_capacity) if target_capacity else 0.0
        else:
            baseline_tickets = max(1, int(round(target_capacity * 0.6)))
            sell_through = baseline_tickets / float(target_capacity) if target_capacity else 0.0

        return baseline_tickets, sell_through

    def _choose_best_ladder(
        self,
        base_ladder: list[Decimal],
        baseline_tickets: int,
        baseline_sell_through: float,
        capacity: int,
    ) -> dict:
        historical_avg = sum(base_ladder) / Decimal(len(base_ladder))
        best = None

        for multiplier in self.MULTIPLIERS:
            candidate_prices = self._ensure_monotonic_prices(
                [self._friendly_price(price * multiplier) for price in base_ladder]
            )
            avg_candidate_price = sum(candidate_prices) / Decimal(len(candidate_prices))
            price_ratio = float(avg_candidate_price / historical_avg) if historical_avg > 0 else 1.0
            demand_factor = max(0.55, min(1.25, math.pow(1 / price_ratio, 1.15))) if price_ratio > 0 else 1.0
            projected_tickets = max(1, min(capacity, int(round(baseline_tickets * demand_factor))))
            projected_sell_through = projected_tickets / float(capacity) if capacity else 0.0
            weighted_revenue = self._weighted_avg_price(candidate_prices) * Decimal(projected_tickets)
            sell_through_gap = max(0.0, (baseline_sell_through * 0.9) - projected_sell_through)
            penalty = Decimal(str(sell_through_gap * capacity)) * historical_avg * Decimal('0.75')
            score = weighted_revenue - penalty
            option = {
                'prices': candidate_prices,
                'projected_tickets': projected_tickets,
                'projected_sell_through_pct': projected_sell_through * 100,
                'score': score,
            }
            if best is None or option['score'] > best['score']:
                best = option

        return best

    def _build_allotments(self, baseline, tier_count: int, capacity: int, projected_tickets: int) -> list[int]:
        stage_shares = self._curve_stage_shares(baseline, tier_count)
        raw = [share * projected_tickets for share in stage_shares]
        allotments = [max(1, int(round(value))) for value in raw]
        total = sum(allotments)
        if total < capacity:
            allotments[-1] += capacity - total
        elif total > capacity:
            overflow = total - capacity
            for idx in range(len(allotments) - 1, -1, -1):
                room = allotments[idx] - 1
                deduction = min(room, overflow)
                allotments[idx] -= deduction
                overflow -= deduction
                if overflow <= 0:
                    break
            if overflow > 0:
                allotments[-1] = max(1, allotments[-1] - overflow)
        return allotments

    def _curve_stage_shares(self, baseline, tier_count: int) -> list[float]:
        if baseline.points_tickets:
            days = sorted(baseline.points_tickets.keys(), reverse=True)
        elif baseline.points:
            days = sorted(baseline.points.keys(), reverse=True)
        else:
            default = {
                2: [0.40, 0.60],
                3: [0.25, 0.35, 0.40],
                4: [0.15, 0.25, 0.30, 0.30],
            }
            return default[tier_count]

        furthest_day = max(days)
        boundaries = []
        for idx in range(1, tier_count):
            ratio = (tier_count - idx) / tier_count
            boundaries.append(int(round(furthest_day * ratio)))

        cumulative = []
        for day in boundaries:
            if baseline.points_tickets:
                cumulative.append(max(0.0, baseline.interpolate_tickets(day)))
            else:
                cumulative.append(max(0.0, baseline.interpolate(day) / 100.0))

        if baseline.points_tickets:
            final_total = max(1.0, baseline.interpolate_tickets(-1))
            cumulative = [min(1.0, value / final_total) for value in cumulative]

        stage_shares = []
        previous = 0.0
        for value in cumulative:
            clamped = max(previous, min(1.0, value))
            stage_shares.append(clamped - previous)
            previous = clamped
        stage_shares.append(max(0.0, 1.0 - previous))

        total = sum(stage_shares)
        if total <= 0:
            return [1 / tier_count] * tier_count
        return [share / total for share in stage_shares]

    def _serialize_comparable_event(self, event: Event, prices: list[Decimal]) -> dict:
        avg_price = (sum(prices) / Decimal(len(prices))).quantize(Decimal('0.01'))
        sell_through = None
        if event.capacity and event.capacity > 0:
            sell_through = round((event.cached_paid_ticket_count / event.capacity) * 100, 1)
        return {
            'name': event.name,
            'event_date': event.start_date.isoformat(),
            'venue_name': f'{event.venue.name}, {event.venue.city}',
            'paid_tickets': int(event.cached_paid_ticket_count or 0),
            'avg_price': self._money_str(avg_price),
            'sell_through_pct': sell_through,
        }

    def _segment_name(self, baseline, event: Event) -> str:
        if baseline.segment_type == 'venue':
            return str(event.venue)
        if baseline.segment_type == 'city':
            return event.venue.city
        return 'All Events'

    def _confidence_label(self, event_count: int, segment_type: str) -> str:
        if event_count >= 6 and segment_type == 'venue':
            return 'high'
        if event_count >= 3:
            return 'medium'
        return 'low'

    def _percentile(self, sorted_prices: list[Decimal], quantile: float) -> Decimal:
        if not sorted_prices:
            return Decimal('0.00')
        index = int(round((len(sorted_prices) - 1) * quantile))
        index = max(0, min(len(sorted_prices) - 1, index))
        return self._friendly_price(sorted_prices[index])

    def _ensure_monotonic_prices(self, prices: list[Decimal]) -> list[Decimal]:
        cleaned = []
        step = Decimal('1.00')
        for idx, price in enumerate(prices):
            price = self._friendly_price(price)
            if idx and price <= cleaned[-1]:
                price = cleaned[-1] + step
            cleaned.append(price)
        return cleaned

    def _friendly_price(self, value: Decimal) -> Decimal:
        value = Decimal(value).quantize(Decimal('0.01'))
        if value <= 0:
            return Decimal('0.00')
        rounded = value.quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        return max(Decimal('1.00'), rounded.quantize(Decimal('0.01')))

    def _weighted_avg_price(self, prices: list[Decimal]) -> Decimal:
        if len(prices) == 2:
            weights = [Decimal('0.40'), Decimal('0.60')]
        elif len(prices) == 4:
            weights = [Decimal('0.15'), Decimal('0.25'), Decimal('0.30'), Decimal('0.30')]
        else:
            weights = [Decimal('0.25'), Decimal('0.35'), Decimal('0.40')]
        return sum(price * weight for price, weight in zip(prices, weights))

    def _money_str(self, value: Decimal) -> str:
        return f'{Decimal(value).quantize(Decimal("0.01"))}'

    def _generate_ai_rationale(
        self,
        *,
        event: Event,
        tier_count: int,
        confidence: str,
        comparable_events: list[dict],
        expected_outcome: dict,
        recommended_tiers: list[dict],
        baseline_info: dict,
    ) -> str:
        if 'test' in sys.argv:
            return ''
        if not getattr(settings, 'OPENAI_API_KEY', ''):
            return ''

        try:
            from langchain_openai import ChatOpenAI
        except Exception:
            return ''

        prompt = self._build_rationale_prompt(
            event=event,
            tier_count=tier_count,
            confidence=confidence,
            comparable_events=comparable_events,
            expected_outcome=expected_outcome,
            recommended_tiers=recommended_tiers,
            baseline_info=baseline_info,
        )
        try:
            llm = ChatOpenAI(
                model=getattr(settings, 'OPENAI_MODEL', 'gpt-4o'),
                api_key=getattr(settings, 'OPENAI_API_KEY', ''),
                temperature=0.2,
            )
            response = llm.invoke([{'role': 'user', 'content': prompt}])
            return (response.content or '').strip()
        except Exception as exc:
            logger.warning('Smart pricing rationale generation failed: %s', exc)
            return ''

    def _build_rationale_prompt(
        self,
        *,
        event: Event,
        tier_count: int,
        confidence: str,
        comparable_events: list[dict],
        expected_outcome: dict,
        recommended_tiers: list[dict],
        baseline_info: dict,
    ) -> str:
        tier_lines = '\n'.join(
            f"- {tier['name']}: ${tier['price_decimal']} for {tier['allotment']} tickets"
            for tier in recommended_tiers
        )
        comparable_lines = '\n'.join(
            f"- {item['name']} ({item['event_date']}) at {item['venue_name']}: "
            f"{item['paid_tickets']} paid tickets, avg ${item['avg_price']}"
            for item in comparable_events[:5]
        )
        return f"""You are writing a short organizer-facing explanation for a ticket pricing recommendation.

Organization: {self.organization.name}
Event: {event.name}
Venue: {event.venue.name}, {event.venue.city}
Event Date: {event.start_date}
Capacity: {event.capacity}
Baseline Segment: {baseline_info['segment_name']}
Comparable Events Used: {baseline_info['events_count']}
Confidence: {confidence}
Tier Count: {tier_count}

Recommended Tiers:
{tier_lines}

Projected Outcome:
- Paid tickets: {expected_outcome['projected_paid_tickets']}
- Sell-through: {expected_outcome['projected_sell_through_pct']}%
- Gross revenue: ${expected_outcome['projected_gross_revenue']}

Comparable Events:
{comparable_lines}

Write 2 short paragraphs. Explain why this ladder is reasonable based on historical sell-through and pricing, and mention the confidence level. Be concise, practical, and avoid hype."""
