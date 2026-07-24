"""
Segment diagnostics and accuracy evaluation (read-only).

RFM segments are rule-derived, not ground-truth labels, so "accuracy" has two
distinct meanings and this service measures both:

  Internal correctness  — do the scoring + rules do what they claim?
      * score histograms per R/F/M (reveals the frequency-score collapse)
      * raw-value stats incl. n_distinct (the smoking gun for tie-collapse)
      * 125-cell R/F/M cube coverage (which cells fall through to Dormant)
      * segment size distribution (over-concentration)

  Predictive validity   — do segments actually predict future behavior?
      A backtest with NO new labels: recompute RFM *as-of* a cutoff T using only
      orders with order_date <= T, then measure realized behavior in the holdout
      window (T, now] per segment (repeat rate, future revenue). A good
      segmentation shows monotonic separation (top segments >> bottom segments),
      scored with a Spearman rank correlation between intended and realized order.

This service NEVER writes Customer rows — the backtest RFM is computed in memory.
It follows the Calculator pattern: SegmentDiagnostics(org).calculate().
"""
from datetime import timedelta
from decimal import Decimal

import numpy as np
from django.db.models import Count, F, Max, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from tickets.models import Customer, TicketOrder
from .rfm_calculator import _assign_score
from .segment_definitions import (
    CANONICAL_VALUE_ORDER,
    FREQUENCY_FEW,
    FREQUENCY_MANY,
    SEGMENT_VALUE_ORDER,
    classify_segment,
    classify_segment_absolute,
    classify_segment_canonical,
    classify_segment_explain,
    derive_monetary_bands,
)

# Below this many non-refunded orders in the holdout window, the backtest can't
# say anything meaningful, so it reports insufficient_holdout instead.
MIN_HOLDOUT_ORDERS = 5

# Above this many segmentable customers, the backtest loads too much into memory
# for a synchronous request. Callers get status "too_large" and are pointed at the
# offline management command instead.
BACKTEST_MAX_CUSTOMERS = 50000

RECENCY_NULL_FALLBACK = 9999  # mirrors RFMCalculator: null last order -> very old


def _percentile_breakpoints(arr):
    """20/40/60/80 percentiles, or median x4 when degenerate. Mirrors RFMCalculator."""
    a = np.array(arr, dtype=float)
    p = np.percentile(a, [20, 40, 60, 80])
    if np.any(np.isnan(p)) or np.all(p == p[0]):
        return [float(np.median(a))] * 4
    return p.tolist()


def _rankdata(vals):
    """Average-rank (ties shared), 1-based. Small reimplementation of scipy.rankdata."""
    arr = np.asarray(vals, dtype=float)
    sorter = np.argsort(arr, kind="mergesort")
    inv = np.empty(len(arr), dtype=int)
    inv[sorter] = np.arange(len(arr))
    arr_sorted = arr[sorter]
    obs = np.r_[True, arr_sorted[1:] != arr_sorted[:-1]]
    dense = obs.cumsum()[inv]
    count = np.r_[np.nonzero(obs)[0], len(arr)]
    return 0.5 * (count[dense] + count[dense - 1] + 1)


def _spearman(x, y):
    """Spearman rank correlation between two equal-length sequences (or None)."""
    if len(x) < 2:
        return None
    rx = _rankdata(x)
    ry = _rankdata(y)
    if np.std(rx) == 0 or np.std(ry) == 0:
        return None
    return round(float(np.corrcoef(rx, ry)[0, 1]), 4)


class SegmentDiagnostics:
    """Evaluate the accuracy/precision of an organization's RFM segment assignments."""

    def __init__(self, organization):
        self.organization = organization

    def _base_queryset(self):
        # Same scope as RFMCalculator: org-scoped, real (non-placeholder) customers.
        return (
            Customer.objects.filter(organization=self.organization)
            .exclude(email__endswith="@placeholder.local")
        )

    # ---- public API ---------------------------------------------------------

    def calculate(self, holdout_days=180, cutoff=None, compare_canonical=False,
                  compare_absolute=False):
        """
        Return the full diagnostics dict:
          {internal, backtest, [internal_canonical_coverage, backtest_canonical],
           [backtest_absolute], cutoff, holdout_days, generated_at}
        cutoff is a date; if omitted, today - holdout_days is used.
        """
        result = {
            "internal": self._internal_diagnostics(),
            "backtest": self._backtest(
                cutoff=cutoff,
                holdout_days=holdout_days,
                rule_set=classify_segment,
                value_order=SEGMENT_VALUE_ORDER,
            ),
            "cutoff": cutoff.isoformat() if cutoff else None,
            "holdout_days": holdout_days,
            "generated_at": timezone.now().isoformat(),
        }
        if compare_canonical:
            result["internal_canonical_coverage"] = self._cube_coverage(
                classify_segment_canonical, explain_fn=None
            )
            result["backtest_canonical"] = self._backtest(
                cutoff=cutoff,
                holdout_days=holdout_days,
                rule_set=classify_segment_canonical,
                value_order=CANONICAL_VALUE_ORDER,
            )
        if compare_absolute:
            result["backtest_absolute"] = self._backtest(
                cutoff=cutoff,
                holdout_days=holdout_days,
                absolute_fn=classify_segment_absolute,
                value_order=SEGMENT_VALUE_ORDER,
            )
        return result

    # ---- internal correctness ----------------------------------------------

    def _internal_diagnostics(self):
        return {
            "score_histograms": self._score_histograms(),
            "raw_value_stats": self._raw_value_stats(),
            "cube_coverage": self._cube_coverage(
                classify_segment, explain_fn=classify_segment_explain
            ),
            "segment_size_distribution": self._segment_size_distribution(),
        }

    def _score_histograms(self):
        """Distribution of the stored 1-5 scores per dimension (reveals collapse)."""
        histograms = {}
        fields = {
            "recency": "rfm_recency_score",
            "frequency": "rfm_frequency_score",
            "monetary": "rfm_monetary_score",
        }
        for label, field in fields.items():
            counts = {i: 0 for i in range(1, 6)}
            rows = (
                self._base_queryset()
                .exclude(**{f"{field}__isnull": True})
                .values(field)
                .annotate(n=Count("id"))
            )
            for row in rows:
                score = row[field]
                if score in counts:
                    counts[score] = row["n"]
            histograms[label] = counts
        return histograms

    def _raw_value_stats(self):
        """Recompute raw R/F/M values as-of-now and report spread + n_distinct."""
        now = timezone.now().date()
        recency_days, order_counts, spends = [], [], []
        rows = (
            self._base_queryset()
            .annotate(order_count=Count("ticket_orders"))
            .values("order_count", "lifetime_value", "last_order_date")
        )
        for row in rows.iterator(chunk_size=2000):
            oc = row["order_count"] or 0
            if oc <= 0:
                continue
            lod = row["last_order_date"]
            recency_days.append((now - lod).days if lod else RECENCY_NULL_FALLBACK)
            order_counts.append(oc)
            spends.append(float(row["lifetime_value"] or 0))

        def stats(arr):
            if not arr:
                return None
            a = np.array(arr, dtype=float)
            p = np.percentile(a, [20, 40, 60, 80])
            return {
                "n": len(arr),
                "min": float(a.min()),
                "p20": float(p[0]),
                "p40": float(p[1]),
                "p60": float(p[2]),
                "p80": float(p[3]),
                "max": float(a.max()),
                "n_distinct": int(np.unique(a).size),
            }

        return {
            "recency": stats(recency_days),
            "frequency": stats(order_counts),
            "monetary": stats(spends),
        }

    def _cube_coverage(self, classify_fn, explain_fn=None):
        """
        Map all 125 (r,f,m) score cells through classify_fn. A cell is "uncovered"
        when explain_fn reports no matching rule (fell through to a catch-all).
        When explain_fn is None (e.g. the canonical grid) every cell is covered.
        """
        cell_map = {}
        segment_cell_counts = {}
        uncovered_cells = []
        total = 0
        covered = 0
        for r in range(1, 6):
            for f in range(1, 6):
                for m in range(1, 6):
                    total += 1
                    name = classify_fn(r, f, m)
                    cell_map[f"{r}-{f}-{m}"] = name
                    segment_cell_counts[name] = segment_cell_counts.get(name, 0) + 1
                    if explain_fn is None:
                        covered += 1
                    else:
                        _, matched_index = explain_fn(r, f, m)
                        if matched_index is not None:
                            covered += 1
                        else:
                            uncovered_cells.append(f"{r}-{f}-{m}")
        return {
            "total_cells": total,
            "covered_cells": covered,
            "coverage_pct": round(100.0 * covered / total, 1) if total else 0.0,
            "uncovered_cells": uncovered_cells,
            "segment_cell_counts": segment_cell_counts,
            "cell_map": cell_map,
        }

    def _segment_size_distribution(self):
        rows = (
            self._base_queryset()
            .exclude(rfm_segment="")
            .values("rfm_segment")
            .annotate(n=Count("id"))
        )
        by_segment = {row["rfm_segment"]: row["n"] for row in rows}
        total = sum(by_segment.values())
        distribution = {}
        over_concentrated = []
        for name, n in sorted(by_segment.items(), key=lambda kv: -kv[1]):
            pct = round(100.0 * n / total, 1) if total else 0.0
            distribution[name] = {"count": n, "pct": pct}
            if pct > 40.0:
                over_concentrated.append(name)
        return {
            "total_scored": total,
            "by_segment": distribution,
            "over_concentrated": over_concentrated,
        }

    def preview_absolute_sizes(self, bands):
        """Segment sizes the org WOULD have under candidate absolute `bands`.

        Read-only, in-memory: one pass over current customers applying
        classify_segment_absolute with the candidate cut-offs. `bands` is an
        (kwargs, monetary_tuple) pair from bands_from_org(). Cheap enough to run
        on every Preview click, but still ceiling-guarded for huge tenants.
        """
        band_kwargs, monetary_bands = bands
        now = timezone.now().date()
        base = (
            self._base_queryset()
            .annotate(order_count=Count("ticket_orders"))
            .values("order_count", "lifetime_value", "last_order_date")
        )
        if base.count() > BACKTEST_MAX_CUSTOMERS:
            return {"status": "too_large", "max_customers": BACKTEST_MAX_CUSTOMERS}

        counts = {}
        total = 0
        for row in base.iterator(chunk_size=2000):
            oc = row["order_count"] or 0
            if oc <= 0:
                seg = "Dormant"
            else:
                lod = row["last_order_date"]
                rd = (now - lod).days if lod else RECENCY_NULL_FALLBACK
                seg = classify_segment_absolute(
                    rd, oc, float(row["lifetime_value"] or 0), monetary_bands, **band_kwargs
                ) or "Dormant"
            counts[seg] = counts.get(seg, 0) + 1
            total += 1

        distribution = {}
        over_concentrated = []
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            pct = round(100.0 * n / total, 1) if total else 0.0
            distribution[name] = {"count": n, "pct": pct}
            if pct > 40.0:
                over_concentrated.append(name)
        return {
            "status": "ok",
            "total_scored": total,
            "by_segment": distribution,
            "over_concentrated": over_concentrated,
        }

    def recommended_bands(self, bands):
        """Data-driven fixes for mis-set 'frequent buyer' / 'top spender' cut-offs.

        One read-only pass over the org's order counts + spend. Returns only the
        levers that are clearly too high (so the top groups are near-empty) with a
        better value, e.g. {'freq_many': 3, 'monetary_high': 65}. Empty when the
        current cut-offs already populate the top groups.
        """
        band_kwargs, monetary_bands = bands
        freq_few = int(band_kwargs.get('freq_few', FREQUENCY_FEW))
        freq_many = int(band_kwargs.get('freq_many', FREQUENCY_MANY))
        mid, high = monetary_bands

        base = (
            self._base_queryset()
            .annotate(order_count=Count('ticket_orders'))
            .values('order_count', 'lifetime_value')
        )
        if base.count() > BACKTEST_MAX_CUSTOMERS:
            return {}

        order_counts = []
        spends = []
        for row in base.iterator(chunk_size=2000):
            oc = row['order_count'] or 0
            if oc > 0:
                order_counts.append(oc)
            lv = float(row['lifetime_value'] or 0)
            if lv > 0:
                spends.append(lv)

        rec = {}

        # Frequent buyer: if the current cut-off makes the 'frequent' group tiny,
        # suggest the smallest count above 'a few' that is a top-~15% (or smaller)
        # slice, falling back to the smallest count that isn't empty.
        if order_counts:
            n = len(order_counts)

            def share_ge(k):
                return sum(1 for c in order_counts if c >= k) / n

            if share_ge(freq_many) < 0.02:
                cand = [k for k in range(freq_few + 1, max(order_counts) + 1) if share_ge(k) > 0]
                top = [k for k in cand if share_ge(k) <= 0.15]
                best = top[0] if top else (cand[0] if cand else None)
                if best and best < freq_many:
                    rec['freq_many'] = best

        # Top spender: if almost no one clears the current amount, suggest ~the
        # 85th percentile of spend (a friendly rounded number).
        if spends and high > 0:
            share_high = sum(1 for s in spends if s >= high) / len(spends)
            if share_high < 0.02:
                p85 = float(np.percentile(np.array(spends, dtype=float), 85))
                p85 = round(p85 / 5) * 5 if p85 >= 20 else round(p85, 2)
                if mid < p85 < high:
                    rec['monetary_high'] = p85

        return rec

    # ---- predictive validity (backtest) ------------------------------------

    def _backtest(self, cutoff=None, holdout_days=180, rule_set=None,
                  value_order=None, absolute_fn=None, bands=None,
                  min_holdout_orders=MIN_HOLDOUT_ORDERS):
        """Backtest a segmentation against realized holdout behavior.

        bands (absolute mode only): an (kwargs, monetary_tuple) pair from
        bands_from_org() to score with fixed per-org cut-offs. When omitted, the
        monetary band is derived from the as-of-T spend distribution.

        By default segments via the score-based rule_set (percentile RFM). Pass
        absolute_fn to instead segment from raw as-of-T values (recency days,
        order count, spend) using fixed behavior-anchored bands.
        """
        rule_set = rule_set or classify_segment
        value_order = value_order or SEGMENT_VALUE_ORDER
        today = timezone.now().date()
        T = cutoff or (today - timedelta(days=holdout_days))

        non_refunded = Q(ticket_orders__refunded_at__isnull=True)
        pre_cut = Q(ticket_orders__order_date__date__lte=T)
        post_cut = Q(ticket_orders__order_date__date__gt=T)

        holdout_order_count = TicketOrder.objects.filter(
            customer__organization=self.organization,
            refunded_at__isnull=True,
            order_date__date__gt=T,
        ).count()
        if holdout_order_count < min_holdout_orders:
            return {
                "status": "insufficient_holdout",
                "cutoff": T.isoformat(),
                "holdout_orders": holdout_order_count,
                "min_holdout_orders": min_holdout_orders,
            }

        # Guard against loading a huge tenant into memory in a request. The
        # offline management command has no such ceiling.
        customer_count = self._base_queryset().count()
        if customer_count > BACKTEST_MAX_CUSTOMERS:
            return {
                "status": "too_large",
                "cutoff": T.isoformat(),
                "customer_count": customer_count,
                "max_customers": BACKTEST_MAX_CUSTOMERS,
            }

        # Net revenue: match Customer.calculate_lifetime_value() — drop fully
        # refunded orders (non_refunded) AND subtract partial refunds.
        net_revenue = F("ticket_orders__total_amount") - F("ticket_orders__refunded_amount")

        # Pass 1: pre-cutoff R/F/M per customer. Cannot use cached
        # last_order_date/lifetime_value — those are as-of-now.
        pre_rows = list(
            self._base_queryset()
            .annotate(
                freq=Count("ticket_orders", filter=non_refunded & pre_cut),
                last_pre=Max("ticket_orders__order_date", filter=non_refunded & pre_cut),
                mon=Coalesce(
                    Sum(net_revenue, filter=non_refunded & pre_cut),
                    Decimal("0.00"),
                ),
            )
            .values("id", "freq", "last_pre", "mon")
        )
        segmented = [row for row in pre_rows if (row["freq"] or 0) > 0]
        if not segmented:
            return {
                "status": "insufficient_holdout",
                "cutoff": T.isoformat(),
                "holdout_orders": holdout_order_count,
                "reason": "no customers with pre-cutoff orders",
            }

        recency_days = [
            (T - row["last_pre"].date()).days if row["last_pre"] else RECENCY_NULL_FALLBACK
            for row in segmented
        ]
        freqs = [row["freq"] for row in segmented]
        spends = [float(row["mon"] or 0) for row in segmented]

        seg_by_customer = {}
        if absolute_fn is not None:
            # Absolute bands: segment straight from raw as-of-T values. With
            # explicit `bands` use the org's stored cut-offs; otherwise derive the
            # monetary band from the as-of-T spend distribution with default
            # recency/frequency cut-offs.
            if bands is not None:
                band_kwargs, monetary_bands = bands
            else:
                band_kwargs, monetary_bands = {}, derive_monetary_bands(spends)
            for row, rd, fr, sp in zip(segmented, recency_days, freqs, spends):
                seg_by_customer[row["id"]] = (
                    absolute_fn(rd, fr, sp, monetary_bands, **band_kwargs) or "Dormant"
                )
        else:
            bp_r = _percentile_breakpoints(recency_days)
            bp_f = _percentile_breakpoints(freqs)
            bp_m = _percentile_breakpoints(spends)
            for row, rd, fr, sp in zip(segmented, recency_days, freqs, spends):
                r = _assign_score(rd, bp_r, lower_is_better=True)
                f = _assign_score(fr, bp_f, lower_is_better=False)
                m = _assign_score(sp, bp_m, lower_is_better=False)
                seg_by_customer[row["id"]] = rule_set(r, f, m) or "Dormant"

        # Holdout realized behavior per customer over (T, now].
        holdout_rows = (
            self._base_queryset()
            .annotate(
                future_orders=Count("ticket_orders", filter=non_refunded & post_cut),
                future_rev=Coalesce(
                    Sum(net_revenue, filter=non_refunded & post_cut),
                    Decimal("0.00"),
                ),
            )
            .values("id", "future_orders", "future_rev")
        )
        holdout_by_customer = {
            row["id"]: (row["future_orders"] or 0, row["future_rev"] or Decimal("0.00"))
            for row in holdout_rows
        }

        # Aggregate per segment.
        agg = {}
        for cust_id, segment in seg_by_customer.items():
            future_orders, future_rev = holdout_by_customer.get(cust_id, (0, Decimal("0.00")))
            bucket = agg.setdefault(
                segment, {"n": 0, "repeat": 0, "rev": Decimal("0.00")}
            )
            bucket["n"] += 1
            bucket["repeat"] += 1 if future_orders > 0 else 0
            bucket["rev"] += future_rev

        per_segment = {}
        for segment, b in agg.items():
            n = b["n"]
            per_segment[segment] = {
                "n": n,
                "repeat_rate": round(b["repeat"] / n, 4) if n else 0.0,
                "avg_future_revenue": round(float(b["rev"]) / n, 2) if n else 0.0,
                "retention_rate": round(b["repeat"] / n, 4) if n else 0.0,
            }

        separation = self._separation_scores(per_segment, value_order)

        return {
            "status": "ok",
            "cutoff": T.isoformat(),
            "holdout_days": holdout_days,
            "n_segmented_customers": len(segmented),
            "holdout_orders": holdout_order_count,
            "per_segment": per_segment,
            "separation": separation,
        }

    def _separation_scores(self, per_segment, value_order):
        """How well realized future behavior follows the intended segment ordering."""
        # Only segments that are both present (n>0) and ranked in value_order.
        ordered = [s for s in value_order if s in per_segment and per_segment[s]["n"] > 0]
        if len(ordered) < 2:
            return {
                "spearman_future_revenue": None,
                "spearman_repeat_rate": None,
                "monotonic_violations": 0,
                "ranked_segments": ordered,
            }
        # Intended goodness: earlier in value_order = better -> higher goodness score.
        intended_goodness = [-value_order.index(s) for s in ordered]
        realized_rev = [per_segment[s]["avg_future_revenue"] for s in ordered]
        realized_repeat = [per_segment[s]["repeat_rate"] for s in ordered]

        violations = 0
        for i in range(len(ordered) - 1):
            if realized_rev[i] < realized_rev[i + 1]:
                violations += 1

        return {
            "spearman_future_revenue": _spearman(intended_goodness, realized_rev),
            "spearman_repeat_rate": _spearman(intended_goodness, realized_repeat),
            "monotonic_violations": violations,
            "ranked_segments": ordered,
        }
