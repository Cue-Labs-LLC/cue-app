"""
RFM (Recency, Frequency, Monetary) calculator.
Computes percentile-based quintiles per organization and assigns segments.
"""
import pandas as pd
from django.db.models import Count, Max, Sum
from django.utils import timezone

from tickets.models import Customer
from .segment_definitions import classify_segment


class RFMCalculator:
    """Compute RFM scores and segments for an organization's customers."""

    def __init__(self, organization):
        self.organization = organization

    def calculate_all(self):
        """
        Compute RFM scores and segments for all customers in the organization.
        Uses percentile-based quintiles (1-5) per org. Recency: fewer days since
        last order = higher score. Updates all customers via bulk_update.
        """
        qs = (
            Customer.objects.filter(organization=self.organization)
            .annotate(
                order_count=Count("ticket_orders"),
                total_spend=Sum("ticket_orders__total_amount"),
                last_order=Max("ticket_orders__order_date"),
            )
        )
        now = timezone.now().date()
        rows = []
        for c in qs:
            rows.append({
                "id": c.id,
                "order_count": c.order_count or 0,
                "total_spend": float(c.total_spend or 0),
                "last_order": c.last_order.date() if c.last_order else None,
            })
        if not rows:
            return

        df = pd.DataFrame(rows)
        has_orders = df["order_count"] > 0

        # Customers with no orders: Dormant, scores 1,1,1
        df["rfm_recency_score"] = None
        df["rfm_frequency_score"] = None
        df["rfm_monetary_score"] = None
        df.loc[~has_orders, "rfm_recency_score"] = 1
        df.loc[~has_orders, "rfm_frequency_score"] = 1
        df.loc[~has_orders, "rfm_monetary_score"] = 1

        if has_orders.any():
            subset = df.loc[has_orders].copy()
            subset["recency_days"] = subset["last_order"].apply(
                lambda d: (now - d).days if d else 9999
            )
            # Quintiles 1-5; recency: lower days = better, so label best bin 5
            try:
                subset["_r_q"] = pd.qcut(
                    subset["recency_days"],
                    q=5,
                    labels=[5, 4, 3, 2, 1],
                    duplicates="drop",
                )
            except Exception:
                subset["_r_q"] = 3
            if hasattr(subset["_r_q"].iloc[0], "item"):
                subset["rfm_recency_score"] = subset["_r_q"].astype(int)
            else:
                subset["rfm_recency_score"] = subset["_r_q"].cat.codes + 1
                subset["rfm_recency_score"] = 6 - subset["rfm_recency_score"]

            try:
                subset["rfm_frequency_score"] = pd.qcut(
                    subset["order_count"],
                    q=5,
                    labels=[1, 2, 3, 4, 5],
                    duplicates="drop",
                )
            except Exception:
                subset["rfm_frequency_score"] = 3
            if hasattr(subset["rfm_frequency_score"].iloc[0], "item"):
                subset["rfm_frequency_score"] = subset["rfm_frequency_score"].astype(int)
            else:
                subset["rfm_frequency_score"] = subset["rfm_frequency_score"].cat.codes + 1

            try:
                subset["rfm_monetary_score"] = pd.qcut(
                    subset["total_spend"],
                    q=5,
                    labels=[1, 2, 3, 4, 5],
                    duplicates="drop",
                )
            except Exception:
                subset["rfm_monetary_score"] = 3
            if hasattr(subset["rfm_monetary_score"].iloc[0], "item"):
                subset["rfm_monetary_score"] = subset["rfm_monetary_score"].astype(int)
            else:
                subset["rfm_monetary_score"] = subset["rfm_monetary_score"].cat.codes + 1

            for col in ("rfm_recency_score", "rfm_frequency_score", "rfm_monetary_score"):
                df.loc[has_orders, col] = subset[col].values

        df["rfm_segment"] = df.apply(
            lambda row: classify_segment(
                row["rfm_recency_score"],
                row["rfm_frequency_score"],
                row["rfm_monetary_score"],
            ),
            axis=1,
        )
        updated_at = timezone.now()
        df["rfm_updated_at"] = updated_at

        # Map back to customer IDs and bulk_update
        customers_to_update = list(
            Customer.objects.filter(
                organization=self.organization,
                id__in=df["id"].tolist(),
            )
        )
        by_id = df.set_index("id")
        for c in customers_to_update:
            r = by_id.loc[c.id]
            c.rfm_recency_score = int(r["rfm_recency_score"]) if r["rfm_recency_score"] is not None else None
            c.rfm_frequency_score = int(r["rfm_frequency_score"]) if r["rfm_frequency_score"] is not None else None
            c.rfm_monetary_score = int(r["rfm_monetary_score"]) if r["rfm_monetary_score"] is not None else None
            c.rfm_segment = (r["rfm_segment"] or "")[:30]
            c.rfm_updated_at = updated_at

        if customers_to_update:
            Customer.objects.bulk_update(
                customers_to_update,
                [
                    "rfm_recency_score",
                    "rfm_frequency_score",
                    "rfm_monetary_score",
                    "rfm_segment",
                    "rfm_updated_at",
                ],
            )
