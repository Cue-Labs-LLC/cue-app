"""Pure, deterministic competition scorer (Phase 1).

``score_competition`` takes already-extracted competitors and turns them into a
0-100 score + Low/Med/High label + breakdown. No I/O, no LLM, no settings-driven
nondeterminism (the window is an explicit argument). This is the metric's math,
pinned by unit tests before any noisy external input (P2/P3) touches it.

See ``docs/technical-design/market-competition-agent.md`` §4.2, §3 and the
decision log (D4, DO2, DO5-b/d/f).
"""
from datetime import timedelta

from .types import CompetitionResult

# Band boundaries (inclusive upper bounds). score <= LOW_MAX => Low;
# <= MEDIUM_MAX => Medium; else High. Pinned by boundary tests (24/25, 59/60).
LOW_MAX = 24
MEDIUM_MAX = 59

# Each in-window, same-metro, genre-matching competitor contributes up to this
# many points (scaled by date closeness), clamped to 100. A lone competitor is
# Low; a dense same-weekend cluster saturates to High.
POINTS_PER_MATCH = 20

# Below this fraction of queries returning usable results, a ~0 score is not a
# trustworthy "you're clear" — report `inconclusive` instead of Low (DO2).
COVERAGE_MIN_RATIO = 0.5


def _label_for_score(score: int) -> str:
    if score <= LOW_MAX:
        return 'Low'
    if score <= MEDIUM_MAX:
        return 'Medium'
    return 'High'


def _norm(value: str) -> str:
    return (value or '').strip().lower()


def _day_distance(target, competitor_date) -> int:
    """Days from ``competitor_date`` to the nearest edge of the target's span.

    0 when the competitor falls within the span itself (DO5-b).
    """
    span_start = target.start_date
    span_end = target.end_date or target.start_date
    if competitor_date < span_start:
        return (span_start - competitor_date).days
    if competitor_date > span_end:
        return (competitor_date - span_end).days
    return 0


def _genre_matches(target, competitor) -> bool:
    """True when the competitor's genre overlaps a target hint (DO5-f fallback
    handled by the caller: empty hints => genre filter dropped entirely)."""
    genre = _norm(competitor.genre)
    if not genre:
        return False
    for hint in target.genre_hints:
        hint = _norm(hint)
        if hint and (hint == genre or hint in genre or genre in hint):
            return True
    return False


def score_competition(target, competitors, undated, coverage, *, window_days) -> CompetitionResult:
    """Score the competitive density around ``target``.

    Filters ``competitors`` to D4 matches (same normalized metro, within
    ±``window_days`` of the date span, genre overlap with a no-genre fallback),
    computes a transparent date-closeness-weighted 0-100 score + band, and
    returns the matches plus the ``undated`` list, ``counts``, ``coverage`` and
    an empty ``summary`` (the narrative is added later, P3).
    """
    undated = list(undated or [])
    target_metro = _norm(target.metro_normalized)
    use_genre = bool(target.genre_hints)  # DO5-f: no confident genre => density only
    window = timedelta(days=window_days)

    dated = []
    for c in (competitors or []):
        if c.date is None:  # DO5-d: no-date events never scored; surfaced separately
            undated.append(c)
        else:
            dated.append(c)

    same_metro = [c for c in dated if _norm(c.metro_normalized) == target_metro]

    span_start = target.start_date
    span_end = target.end_date or target.start_date
    in_window = [
        c for c in same_metro
        if (span_start - window) <= c.date <= (span_end + window)
    ]

    if use_genre:
        matched = [c for c in in_window if _genre_matches(target, c)]
    else:
        matched = list(in_window)

    raw = 0.0
    for c in matched:
        distance = _day_distance(target, c.date)
        closeness = 1.0 - (distance / (window_days + 1))  # (0, 1]; 1.0 within span
        raw += closeness
    score = min(100, round(raw * POINTS_PER_MATCH))
    label = _label_for_score(score)

    coverage = dict(coverage or {})
    ratio = coverage.get('ratio', 1.0)
    if score == 0 and ratio < COVERAGE_MIN_RATIO:
        status = 'inconclusive'  # DO2
    else:
        status = 'ready'

    counts = {
        'total': len(dated) + len(undated),
        'same_metro': len(same_metro),
        'in_window': len(in_window),
        'genre_matched': len(matched),
        'undated': len(undated),
    }

    return CompetitionResult(
        score=score,
        label=label,
        status=status,
        competitors=matched,
        undated=undated,
        counts=counts,
        coverage=coverage,
        summary='',
    )
