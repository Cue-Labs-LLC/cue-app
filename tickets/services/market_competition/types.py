"""Data structures for the Market Competition Agent.

These are lightweight in-memory dataclasses used by the pure scoring and
query-plan layers (Phase 1) — not Django ORM models. Each provides a
``to_dict()`` serializer so later phases can persist a result straight into
``Event.competition_data`` (and rehydrate it for rendering without a recompute).

See ``docs/technical-design/market-competition-agent.md`` §4.2.
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class CompetitorEvent:
    """One competing event found in the market around the target event's date.

    ``date`` is ``None`` when extraction could not confidently pin a date; such
    rows are excluded from the score and surfaced separately (DO5-d).
    ``metro_normalized`` is the LLM-normalized metro used for deterministic
    metro matching in the scorer (DO5-e).
    """
    name: str
    date: Optional[date]
    venue_name: str
    metro_normalized: str
    genre: str
    platform: str
    source_url: str

    def to_dict(self) -> dict:
        """Serialize for JSON persistence (date as ISO string or ``None``)."""
        return {
            'name': self.name,
            'date': self.date.isoformat() if self.date else None,
            'venue_name': self.venue_name,
            'metro_normalized': self.metro_normalized,
            'genre': self.genre,
            'platform': self.platform,
            'source_url': self.source_url,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'CompetitorEvent':
        """Rebuild from a ``to_dict()`` payload (ISO date string or ``None``)."""
        raw_date = data.get('date')
        return cls(
            name=data.get('name', ''),
            date=date.fromisoformat(raw_date) if raw_date else None,
            venue_name=data.get('venue_name', ''),
            metro_normalized=data.get('metro_normalized', ''),
            genre=data.get('genre', ''),
            platform=data.get('platform', ''),
            source_url=data.get('source_url', ''),
        )


@dataclass
class TargetEvent:
    """The scorer's ORM-free view of the event being scanned.

    Keeps ``score_competition`` pure and unit-testable. The Phase-3 calculator
    builds this from the real ``Event`` plus the LLM-normalized metro (DO5-e).
    ``genre_hints`` empty => the no-confident-genre fallback applies (DO5-f).
    """
    metro_normalized: str
    start_date: date
    end_date: Optional[date] = None
    genre_hints: list = field(default_factory=list)


@dataclass
class CompetitionResult:
    """Outcome of one competition scan (the panel renders straight from this)."""
    score: int
    label: str
    status: str
    competitors: list = field(default_factory=list)   # scored (dated) matches
    undated: list = field(default_factory=list)        # found but undateable (DO5-d)
    counts: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)
    summary: str = ''

    def to_dict(self) -> dict:
        """Serialize for persistence into ``Event.competition_data``."""
        return {
            'score': self.score,
            'label': self.label,
            'status': self.status,
            'competitors': [c.to_dict() for c in self.competitors],
            'undated': [c.to_dict() for c in self.undated],
            'counts': self.counts,
            'coverage': self.coverage,
            'summary': self.summary,
        }
