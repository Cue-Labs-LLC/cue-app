"""Loyalty program dashboard statistics.

Returns the per-tier membership distribution for a program's detail page in a
single group-by pass (``.values().annotate()``), following the aggregation
guidance in CLAUDE.md.
"""
from decimal import Decimal

from django.db.models import Avg, Count

from tickets.models import Customer

PLACEHOLDER_EMAIL_SUFFIX = '@placeholder.local'


class LoyaltyProgramStats:
    """Compute tier distribution + summary for one ``LoyaltyProgram``."""

    def __init__(self, program):
        self.program = program
        self.organization = program.organization

    def calculate(self):
        base = (
            Customer.objects.filter(organization=self.organization)
            .exclude(email__endswith=PLACEHOLDER_EMAIL_SUFFIX)
        )
        total_customers = base.count()

        # One group-by pass: members + avg LTV per tier (loyalty_tier=None -> unassigned).
        rows = (
            base.values('loyalty_tier')
            .annotate(count=Count('id'), avg_ltv=Avg('lifetime_value'))
        )
        by_tier = {row['loyalty_tier']: row for row in rows}

        tiers = []
        assigned = 0
        for tier in self.program.tiers.all().order_by('-rank', 'name'):
            row = by_tier.get(tier.id, {})
            count = row.get('count', 0)
            assigned += count
            tiers.append({
                'tier': tier,
                'count': count,
                'pct': round(100.0 * count / total_customers, 1) if total_customers else 0,
                'avg_ltv': row.get('avg_ltv') or Decimal('0.00'),
            })

        unassigned = by_tier.get(None, {}).get('count', 0)
        return {
            'tiers': tiers,
            'total_customers': total_customers,
            'assigned': assigned,
            'unassigned': unassigned,
            'enrolled_pct': round(100.0 * assigned / total_customers, 1) if total_customers else 0,
        }
