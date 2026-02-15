"""
RFM (Recency, Frequency, Monetary) calculator.
Computes percentile-based quintiles per organization and assigns segments.
Uses chunked two-pass processing for bounded memory with large datasets.
"""
import numpy as np
from django.db.models import Count, Max, Sum
from django.utils import timezone

from tickets.models import Customer
from .segment_definitions import classify_segment

CHUNK_SIZE = 2000
RFM_UPDATE_FIELDS = [
    "rfm_recency_score",
    "rfm_frequency_score",
    "rfm_monetary_score",
    "rfm_segment",
    "rfm_updated_at",
]


def _assign_score(value, breakpoints, lower_is_better=False):
    """
    Assign quintile score 1-5 from value and four breakpoints (20th, 40th, 60th, 80th percentiles).
    If breakpoints are empty or all equal, return 3.
    lower_is_better: True for recency (fewer days = higher score).
    """
    if value is None or breakpoints is None or len(breakpoints) == 0:
        return 3
    b = breakpoints
    if lower_is_better:
        # Recency: lower value -> higher score (5 = best)
        if value <= b[0]:
            return 5
        if value <= b[1]:
            return 4
        if value <= b[2]:
            return 3
        if value <= b[3]:
            return 2
        return 1
    else:
        # Frequency/Monetary: higher value -> higher score (5 = best)
        if value <= b[0]:
            return 1
        if value <= b[1]:
            return 2
        if value <= b[2]:
            return 3
        if value <= b[3]:
            return 4
        return 5


class RFMCalculator:
    """Compute RFM scores and segments for an organization's customers."""

    def __init__(self, organization):
        self.organization = organization

    def _base_queryset(self):
        return Customer.objects.filter(organization=self.organization).annotate(
            order_count=Count("ticket_orders"),
            total_spend=Sum("ticket_orders__total_amount"),
            last_order=Max("ticket_orders__order_date"),
        )

    def _compute_breakpoints(self, now):
        """
        Pass 1: Stream all customers, collect recency_days, order_count, total_spend
        for customers with orders. Compute 20/40/60/80 percentiles for quintile boundaries.
        Returns (breakpoints_r, breakpoints_f, breakpoints_m) or (None, None, None) if no orders.
        """
        recency_days = []
        order_counts = []
        total_spends = []
        qs = self._base_queryset()
        for c in qs.iterator(chunk_size=CHUNK_SIZE):
            oc = c.order_count or 0
            if oc <= 0:
                continue
            recency_days.append(
                (now - c.last_order.date()).days if c.last_order else 9999
            )
            order_counts.append(oc)
            total_spends.append(float(c.total_spend or 0))

        if not recency_days:
            return None, None, None

        def percentiles(arr):
            a = np.array(arr, dtype=float)
            p = np.percentile(a, [20, 40, 60, 80])
            if np.any(np.isnan(p)) or np.all(p == p[0]):
                return None
            return p.tolist()

        breakpoints_r = percentiles(recency_days)
        breakpoints_f = percentiles(order_counts)
        breakpoints_m = percentiles(total_spends)
        if breakpoints_r is None:
            breakpoints_r = [np.median(recency_days)] * 4
        if breakpoints_f is None:
            breakpoints_f = [np.median(order_counts)] * 4
        if breakpoints_m is None:
            breakpoints_m = [np.median(total_spends)] * 4
        return breakpoints_r, breakpoints_f, breakpoints_m

    def _score_customer(self, customer, now, breakpoints_r, breakpoints_f, breakpoints_m):
        """Assign R, F, M scores (1-5) for one customer. Returns (r, f, m)."""
        oc = customer.order_count or 0
        if oc <= 0:
            return 1, 1, 1
        recency_days = (
            (now - customer.last_order.date()).days if customer.last_order else 9999
        )
        r = _assign_score(recency_days, breakpoints_r, lower_is_better=True)
        f = _assign_score(oc, breakpoints_f, lower_is_better=False)
        m = _assign_score(float(customer.total_spend or 0), breakpoints_m, lower_is_better=False)
        return r, f, m

    def calculate_all(self):
        """
        Compute RFM scores and segments for all customers in the organization.
        Two-pass chunked: pass 1 computes org-wide quintile breakpoints; pass 2
        streams customers in chunks, assigns scores, and bulk_updates each chunk.
        """
        now = timezone.now().date()
        updated_at = timezone.now()

        breakpoints_r, breakpoints_f, breakpoints_m = self._compute_breakpoints(now)
        has_breakpoints = breakpoints_r is not None

        qs = self._base_queryset()
        chunk = []
        for customer in qs.iterator(chunk_size=CHUNK_SIZE):
            if has_breakpoints:
                r, f, m = self._score_customer(
                    customer, now, breakpoints_r, breakpoints_f, breakpoints_m
                )
            else:
                r = f = m = 1
            segment = (classify_segment(r, f, m) or "Dormant")[:30]
            customer.rfm_recency_score = r
            customer.rfm_frequency_score = f
            customer.rfm_monetary_score = m
            customer.rfm_segment = segment
            customer.rfm_updated_at = updated_at
            chunk.append(customer)
            if len(chunk) >= CHUNK_SIZE:
                Customer.objects.bulk_update(chunk, RFM_UPDATE_FIELDS)
                chunk = []
        if chunk:
            Customer.objects.bulk_update(chunk, RFM_UPDATE_FIELDS)
