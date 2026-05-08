import json
import logging
from datetime import date
from datetime import timedelta

from django.conf import settings
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class CampaignCandidate(BaseModel):
    campaign_id: str
    confidence: float = Field(ge=0, le=1)
    reasoning: str


class MatchResult(BaseModel):
    candidates: list[CampaignCandidate] = Field(default_factory=list)


class MetaCampaignMatcher:
    """Ranks Meta campaigns against a Cue event using the configured OpenAI model."""

    def __init__(self, organization):
        self.organization = organization

    def rank(self, event, campaigns: list[dict]) -> MatchResult:
        if not campaigns:
            return MatchResult()

        filtered_campaigns = self._prefilter_campaigns(event, campaigns)
        prompt = self._build_prompt(event, filtered_campaigns)

        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(
            model=getattr(settings, "OPENAI_MODEL", "gpt-4o"),
            api_key=getattr(settings, "OPENAI_API_KEY", ""),
            temperature=0.0,
        )
        structured_llm = llm.with_structured_output(MatchResult)
        result = structured_llm.invoke([
            {
                "role": "system",
                "content": (
                    "Match Meta Ads campaigns to an event. Rank by name overlap, date proximity, "
                    "venue/city hints, and objective relevance. Return up to 3 candidates sorted "
                    "by confidence. Use confidence below 0.3 for weak matches."
                ),
            },
            {"role": "user", "content": prompt},
        ])
        if isinstance(result, MatchResult):
            return self._limit_result(result)
        if hasattr(MatchResult, "model_validate"):
            return self._limit_result(MatchResult.model_validate(result))
        return self._limit_result(MatchResult.parse_obj(result))

    def _prefilter_campaigns(self, event, campaigns: list[dict]) -> list[dict]:
        if len(campaigns) <= 50:
            return campaigns

        window_start = event.start_date - timedelta(days=60)
        window_end = event.start_date + timedelta(days=30)
        filtered = []
        for campaign in campaigns:
            campaign_start = _parse_date(campaign.get("start_time") or campaign.get("created_time"))
            campaign_stop = _parse_date(campaign.get("stop_time")) or window_end
            if campaign_start and campaign_start <= window_end and campaign_stop >= window_start:
                filtered.append(campaign)

        return filtered[:50] if filtered else campaigns[:50]

    def _build_prompt(self, event, campaigns: list[dict]) -> str:
        venue = getattr(event, "venue", None)
        event_payload = {
            "name": event.name,
            "summary": event.summary,
            "start_date": event.start_date.isoformat() if event.start_date else None,
            "end_date": event.end_date.isoformat() if event.end_date else None,
            "venue": {
                "name": getattr(venue, "name", ""),
                "city": getattr(venue, "city", ""),
                "state": getattr(venue, "state", ""),
            },
        }
        campaign_payload = [
            {
                "id": campaign.get("id"),
                "name": campaign.get("name"),
                "objective": campaign.get("objective"),
                "status": campaign.get("status"),
                "created_time": campaign.get("created_time"),
                "start_time": campaign.get("start_time"),
                "stop_time": campaign.get("stop_time"),
            }
            for campaign in campaigns
        ]
        return (
            "Event:\n"
            f"{json.dumps(event_payload, indent=2)}\n\n"
            "Campaigns:\n"
            f"{json.dumps(campaign_payload, indent=2)}"
        )

    @staticmethod
    def _limit_result(result: MatchResult) -> MatchResult:
        candidates = sorted(result.candidates, key=lambda item: item.confidence, reverse=True)[:3]
        return MatchResult(candidates=candidates)


def _parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10]) if isinstance(value, str) else None
    except (TypeError, ValueError, IndexError):
        return None
