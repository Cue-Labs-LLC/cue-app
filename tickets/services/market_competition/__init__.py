"""Market Competition Agent — per-event competitive-density signal.

Phase 1 ships the pure, offline core: the scoring math (``scoring``) and the
fixed search-query plan (``query_plan``), over the dataclasses in ``types``.
Later phases add the Celery task + persistence (P4) and the event-detail panel
(P5). P3 adds the LLM scanner + calculator and the single public entry point
``calculate_event_competition``.

See ``docs/technical-design/market-competition-agent.md``.
"""
from .calculator import (
    MarketCompetitionCalculator,
    calculate_event_competition,
    derive_genre_hints,
)
from .query_plan import build_queries
from .scanner import scan_competitors
from .scoring import score_competition
from .search_client import search_queries, web_search
from .types import CompetitionResult, CompetitorEvent, TargetEvent

__all__ = [
    'CompetitorEvent',
    'TargetEvent',
    'CompetitionResult',
    'score_competition',
    'build_queries',
    'web_search',
    'search_queries',
    'scan_competitors',
    'derive_genre_hints',
    'MarketCompetitionCalculator',
    'calculate_event_competition',
]
