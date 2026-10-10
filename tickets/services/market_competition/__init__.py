"""Market Competition Agent — per-event competitive-density signal.

Phase 1 ships the pure, offline core: the scoring math (``scoring``) and the
fixed search-query plan (``query_plan``), over the dataclasses in ``types``.
Later phases add the search client (P2), LLM scanner + calculator (P3), the
Celery task + persistence (P4), and the event-detail panel (P5). The single
public entry point ``calculate_event_competition`` is introduced in P3.

See ``docs/technical-design/market-competition-agent.md``.
"""
from .query_plan import build_queries
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
]
