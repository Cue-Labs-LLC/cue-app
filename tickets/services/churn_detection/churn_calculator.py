from datetime import timedelta
from decimal import Decimal

from django.db.models import Case, CharField, Count, F, OuterRef, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from tickets.models import Customer, TicketOrder


THRESHOLD_OPTIONS = [30, 60, 90, 180]


class ChurnDetectionService:
    """Find customers who were previously active and have since gone quiet."""

    def __init__(self, organization):
        self.organization = organization

    def calculate(self, days_threshold=90):
        cutoff = timezone.now().date() - timedelta(days=days_threshold)

        order_count_subquery = Subquery(
            TicketOrder.objects.filter(
                customer=OuterRef('pk'),
                is_in_person=False,
            )
            .values('customer')
            .annotate(count=Count('id'))
            .values('count')
        )

        customers = (
            Customer.objects.filter(organization=self.organization)
            .exclude(email__endswith='@placeholder.local')
            .filter(last_order_date__lt=cutoff)
            .annotate(order_count=Coalesce(order_count_subquery, 0))
            .filter(order_count__gte=2)
            .order_by('-lifetime_value', 'name')
        )

        aggregate = customers.aggregate(
            total_count=Count('id'),
            total_ltv=Sum('lifetime_value'),
        )
        total_count = aggregate['total_count'] or 0
        total_ltv = aggregate['total_ltv'] or Decimal('0.00')
        avg_ltv = total_ltv / total_count if total_count else Decimal('0.00')

        segment_breakdown = list(
            customers
            .annotate(
                seg=Case(
                    When(rfm_segment='', then=Value('Dormant')),
                    When(rfm_segment__isnull=True, then=Value('Dormant')),
                    default=F('rfm_segment'),
                    output_field=CharField(),
                )
            )
            .values('seg')
            .annotate(count=Count('id'))
            .order_by('-count', 'seg')
        )

        return {
            'customers': customers,
            'stats': {
                'total_count': total_count,
                'total_ltv_at_risk': total_ltv,
                'avg_ltv': avg_ltv,
                'segment_breakdown': segment_breakdown,
            },
        }
