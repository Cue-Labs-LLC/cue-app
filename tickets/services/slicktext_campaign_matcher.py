import json
from datetime import date, timedelta

from django.conf import settings
from pydantic import BaseModel, Field

from tickets.models import AITokenUsage
from tickets.services.ai_metering import record_ai_token_usage


class SlickTextCampaignCandidate(BaseModel):
    campaign_id: str
    confidence: float = Field(ge=0, le=1)
    reasoning: str


class SlickTextMatchResult(BaseModel):
    candidates: list[SlickTextCampaignCandidate] = Field(default_factory=list)


class SlickTextCampaignMatcher:
    """Ranks SlickText SMS broadcasts against a Cue event using the configured OpenAI model."""

    def __init__(self, organization):
        self.organization = organization

    def rank(self, event, campaigns: list[dict]) -> SlickTextMatchResult:
        if not campaigns:
            return SlickTextMatchResult()

        filtered = self._prefilter(event, campaigns)
        prompt = self._build_prompt(event, filtered)

        from langchain_openai import ChatOpenAI

        model_name = getattr(settings, "OPENAI_MODEL", "gpt-4o")
        llm = ChatOpenAI(
            model=model_name,
            api_key=getattr(settings, "OPENAI_API_KEY", ""),
            temperature=0.0,
            stream_usage=True,
        )
        structured_llm = llm.with_structured_output(SlickTextMatchResult, include_raw=True)
        raw_result = structured_llm.invoke([
            {
                "role": "system",
                "content": (
                    "Match SlickText SMS broadcasts to an event. Rank by event name overlap, "
                    "blast name and message-body hints, date proximity, and venue/city hints. "
                    "Return up to 10 candidates sorted by confidence. Use confidence below 0.3 for weak matches."
                ),
            },
            {"role": "user", "content": prompt},
        ])

        if isinstance(raw_result, dict) and {'raw', 'parsed', 'parsing_error'} <= set(raw_result):
            record_ai_token_usage(
                organization=self.organization,
                feature=AITokenUsage.FEATURE_SLICKTEXT_CAMPAIGN_MATCH,
                model_name=model_name,
                usage=raw_result.get('raw'),
                metadata={
                    'event_id': str(event.id),
                    'campaign_count': len(filtered),
                },
            )
            if raw_result.get('parsing_error'):
                raise raw_result['parsing_error']
            result = raw_result.get('parsed')
        else:
            result = raw_result

        if isinstance(result, SlickTextMatchResult):
            return self._limit_result(result)
        if hasattr(SlickTextMatchResult, "model_validate"):
            return self._limit_result(SlickTextMatchResult.model_validate(result))
        return self._limit_result(SlickTextMatchResult.parse_obj(result))

    def _prefilter(self, event, campaigns: list[dict]) -> list[dict]:
        if len(campaigns) <= 50:
            return campaigns

        window_start = event.start_date - timedelta(days=90)
        window_end = event.start_date + timedelta(days=7)
        filtered = []
        for campaign in campaigns:
            sent_date = _parse_date(
                campaign.get("finished")
                or campaign.get("started")
                or campaign.get("scheduled")
            )
            if sent_date and window_start <= sent_date <= window_end:
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
                "id": campaign.get("campaign_id") or campaign.get("id"),
                "name": campaign.get("name"),
                "body": campaign.get("body") or campaign.get("message"),
                "status": campaign.get("status"),
                "scheduled": campaign.get("scheduled"),
                "started": campaign.get("started"),
                "finished": campaign.get("finished"),
                "audience_size": campaign.get("audience_size"),
            }
            for campaign in campaigns
        ]
        return (
            "Event:\n"
            f"{json.dumps(event_payload, indent=2)}\n\n"
            "SlickText SMS broadcasts:\n"
            f"{json.dumps(campaign_payload, indent=2, default=str)}"
        )

    @staticmethod
    def _limit_result(result: SlickTextMatchResult) -> SlickTextMatchResult:
        candidates = sorted(result.candidates, key=lambda item: item.confidence, reverse=True)[:10]
        return SlickTextMatchResult(candidates=candidates)


def _parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10]) if isinstance(value, str) else None
    except (TypeError, ValueError, IndexError):
        return None
