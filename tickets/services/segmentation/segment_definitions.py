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
