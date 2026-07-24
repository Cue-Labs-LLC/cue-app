"""
RFM segment definitions and classification.
First-match-wins rule order; badge colors for Bootstrap/UI.
"""

# (segment_name, (r_min, r_max), (f_min, f_max), (m_min, m_max))
# None for min/max means "any"
SEGMENT_RULES = [
    ("VIP", (4, 5), (4, 5), (4, 5)),
    ("Loyal", (3, 5), (3, 5), (3, 5)),
    ("Big Spender", (3, 5), (1, 3), (4, 5)),
    ("New", (4, 5), (1, 1), (1, 3)),
    ("Promising", (3, 4), (1, 2), (1, 3)),
    ("At-Risk", (2, 3), (3, 5), (3, 5)),
    ("Lapsed", (1, 2), (2, 5), None),  # M any
    ("Dormant", (1, 2), (1, 1), (1, 2)),
]

SEGMENT_BADGE_COLORS = {
    "VIP": "danger",
    "Loyal": "success",
    "Big Spender": "info",
    "New": "primary",
    "Promising": "primary",
    "At-Risk": "warning",
    "Lapsed": "warning",
    "Dormant": "secondary",
}

# Plain-English descriptions for UI (one short sentence per segment).
SEGMENT_DESCRIPTIONS = {
    "VIP": "Your best customers: they buy often, recently, and spend a lot.",
    "Loyal": "Regular customers who order often and spend well.",
    "Big Spender": "High spenders who don't order very often, but when they do they spend a lot.",
    "New": "Recently active with only one order so far; low spend.",
    "Promising": "Somewhat recent, few orders, moderate spend—could grow.",
    "At-Risk": "Used to be active but haven't bought recently; good order history.",
    "Lapsed": "Haven't bought in a while but had multiple orders before.",
    "Dormant": "Inactive, one or no orders, low spend.",
}


def _in_range(value, min_max):
    if value is None:
        return False
    lo, hi = min_max
    return lo <= value <= hi


def classify_segment(r, f, m):
    """
    Classify a customer into one segment from R/F/M scores (1-5).
    Returns segment name string; None if no match (e.g. null scores).
    """
    if r is None or f is None or m is None:
        return "Dormant"  # no-order or unscored -> Dormant
    for name, r_range, f_range, m_range in SEGMENT_RULES:
        if not _in_range(r, r_range):
            continue
        if not _in_range(f, f_range):
            continue
        if m_range is None or _in_range(m, m_range):
            return name
    return "Dormant"


def classify_segment_explain(r, f, m):
    """
    Like classify_segment but also reports which SEGMENT_RULES index matched.

    Returns (segment_name, matched_index). matched_index is the index into
    SEGMENT_RULES of the rule that fired, or None if no rule matched and the
    customer fell through to the "Dormant" catch-all. Used by the diagnostics
    cube-coverage audit to distinguish an explicit Dormant match from a
    silent fall-through.
    """
    if r is None or f is None or m is None:
        return "Dormant", None
    for idx, (name, r_range, f_range, m_range) in enumerate(SEGMENT_RULES):
        if not _in_range(r, r_range):
            continue
        if not _in_range(f, f_range):
            continue
        if m_range is None or _in_range(m, m_range):
            return name, idx
    return "Dormant", None


# A-priori "value" ordering (most valuable first) for the current 8 segments.
# Used by the diagnostics backtest to test whether realized future behavior
# actually follows the intended segment quality ranking.
SEGMENT_VALUE_ORDER = [
    "VIP", "Loyal", "Big Spender", "At-Risk", "Promising", "New", "Lapsed", "Dormant",
]


# --- Candidate canonical 11-segment grid (comparison only) ---------------------
# Standard RFM segment grid (Champions, Loyal Customers, ...). NOT wired into the
# production RFMCalculator; used only by SegmentDiagnostics as an alternate
# rule_set so the eventual decision to adopt it can be driven by measured
# separation numbers. Exhaustive by construction: every (recency, FM) cell of the
# 5x5 grid maps to exactly one segment, so cube coverage is 100%.
#
# Rows are Recency score (5 = most recent, top) and columns are the combined
# Frequency+Monetary score FM = round((F + M) / 2), 1..5 left-to-right.
CANONICAL_SEGMENT_GRID = {
    5: ["New Customers", "Potential Loyalist", "Potential Loyalist", "Loyal Customers", "Champions"],
    4: ["Promising", "Potential Loyalist", "Loyal Customers", "Loyal Customers", "Champions"],
    3: ["About To Sleep", "Need Attention", "Loyal Customers", "Loyal Customers", "Champions"],
    2: ["Hibernating", "About To Sleep", "At Risk", "At Risk", "Can't Lose Them"],
    1: ["Lost", "Hibernating", "At Risk", "Can't Lose Them", "Can't Lose Them"],
}

CANONICAL_VALUE_ORDER = [
    "Champions", "Loyal Customers", "Potential Loyalist", "New Customers",
    "Promising", "Need Attention", "At Risk", "Can't Lose Them",
    "About To Sleep", "Hibernating", "Lost",
]


# --- Candidate absolute / behavior-anchored segmentation (comparison only) -----
# Unlike the population-relative RFM quintiles, these are FIXED thresholds: a
# customer only changes band when their own behavior changes, not when the cohort
# shifts. Recency/frequency bands need no seeding. Monetary bands are seeded once
# from the org's own spend distribution and then held fixed (see derive_monetary_bands).
# Reuses the existing 8 segment names so it is a drop-in for the current UI/filters.
RECENCY_ACTIVE_DAYS = 90       # <= active
RECENCY_COOLING_DAYS = 180     # 91..180 cooling; >180 lapsed/lost
FREQUENCY_MANY = 5             # >= many
FREQUENCY_FEW = 2              # 2..4 few; <=1 one


def derive_monetary_bands(spends):
    """Seed (mid, high) monetary thresholds once from a spend distribution.

    Returns fixed currency thresholds. In production these are stored at seed
    time so the bands stay stable across runs — that stability is the whole
    point of absolute scoring. Uses the 60th/85th percentiles of positive spend.
    """
    import numpy as np
    positive = [float(s) for s in spends if s and float(s) > 0]
    if not positive:
        return (0.0, 0.0)
    mid, high = np.percentile(np.array(positive, dtype=float), [60, 85])
    mid, high = float(mid), float(high)
    if high <= mid:
        high = mid + 1.0
    return (mid, high)


def default_segment_bands():
    """The module-default absolute cut-offs (monetary seeded per-org separately)."""
    return {
        "recency_active_days": RECENCY_ACTIVE_DAYS,
        "recency_cooling_days": RECENCY_COOLING_DAYS,
        "freq_few": FREQUENCY_FEW,
        "freq_many": FREQUENCY_MANY,
        "monetary_mid": 0.0,
        "monetary_high": 0.0,
    }


def bands_from_org(org):
    """Build absolute-mode inputs from org.segment_bands, defaulting per key.

    Returns (kwargs, monetary_bands) where kwargs feeds classify_segment_absolute's
    recency/frequency thresholds and monetary_bands is the (mid, high) tuple.
    """
    stored = org.segment_bands or {}
    d = default_segment_bands()

    def _as_int(key):
        # int(float(...)) tolerates decimal strings from a corrupted JSON blob so
        # a malformed value never crashes the recalc task mid-run.
        try:
            return int(float(stored.get(key, d[key])))
        except (TypeError, ValueError):
            return int(d[key])

    kwargs = {
        "recency_active": _as_int("recency_active_days"),
        "recency_cooling": _as_int("recency_cooling_days"),
        "freq_few": _as_int("freq_few"),
        "freq_many": _as_int("freq_many"),
    }
    monetary_bands = (
        float(stored.get("monetary_mid", d["monetary_mid"])),
        float(stored.get("monetary_high", d["monetary_high"])),
    )
    return kwargs, monetary_bands


def seed_segment_bands(org, force=False):
    """Seed org.segment_bands from the org's current spend distribution and persist.

    Monetary mid/high come from derive_monetary_bands over positive lifetime_value;
    recency/frequency come from module defaults. No-op if already seeded unless force.
    Returns the resulting bands dict.
    """
    from tickets.models import Customer  # local import avoids circular import
    if org.segment_bands and not force:
        return org.segment_bands
    spends = list(
        Customer.objects.filter(organization=org)
        .exclude(email__endswith="@placeholder.local")
        .values_list("lifetime_value", flat=True)
    )
    mid, high = derive_monetary_bands(spends)
    d = default_segment_bands()
    bands = {
        "recency_active_days": d["recency_active_days"],
        "recency_cooling_days": d["recency_cooling_days"],
        "freq_few": d["freq_few"],
        "freq_many": d["freq_many"],
        "monetary_mid": round(mid, 2),
        "monetary_high": round(high, 2),
    }
    org.segment_bands = bands
    org.save(update_fields=["segment_bands"])
    return bands


def classify_segment_absolute(recency_days, freq, monetary, monetary_bands,
                              recency_active=RECENCY_ACTIVE_DAYS,
                              recency_cooling=RECENCY_COOLING_DAYS,
                              freq_many=FREQUENCY_MANY, freq_few=FREQUENCY_FEW):
    """Behavior-anchored absolute-threshold segment from raw as-of values.

    recency_days: days since last order; freq: order count; monetary: spend;
    monetary_bands: (mid, high) currency thresholds from derive_monetary_bands.
    recency_active/recency_cooling/freq_many/freq_few: per-org cut-offs (default to
    the module constants when not supplied).
    """
    mid, high = monetary_bands
    freq = freq or 0
    monetary = float(monetary or 0)
    active = recency_days <= recency_active
    cooling = recency_active < recency_days <= recency_cooling
    many = freq >= freq_many
    few = freq_few <= freq < freq_many
    # Money bands must be real thresholds: with a degenerate all-zero band nobody
    # clears them (guards against everyone landing in VIP/Big Spender/Promising).
    high_val = high > 0 and monetary >= high
    mid_val = mid > 0 and monetary >= mid  # "decent spend"

    if active:
        if many and high_val:
            return "VIP"
        if many:
            return "Loyal"
        if high_val:
            return "Big Spender"
        # A few orders OR a single decent-spend order -> worth nurturing.
        if few or mid_val:
            return "Promising"
        return "New"
    if cooling:
        # Had real engagement (repeat orders or decent spend) -> worth winning back.
        if many or few or mid_val:
            return "At-Risk"
        return "Promising"
    if freq >= freq_few:
        return "Lapsed"
    return "Dormant"


def classify_segment_canonical(r, f, m):
    """
    Classify into the canonical 11-segment grid from R/F/M scores (1-5).
    Combines F and M into FM = round((F + M) / 2), then looks up the
    (recency, FM) cell. Exhaustive: every score triple maps to a segment.
    """
    if r is None or f is None or m is None:
        return "Lost"
    fm = int(round((f + m) / 2.0))
    fm = max(1, min(5, fm))
    r = max(1, min(5, r))
    return CANONICAL_SEGMENT_GRID[r][fm - 1]
