import json
from datetime import date, timedelta

from django.conf import settings
from pydantic import BaseModel, Field


class MailchimpCampaignCandidate(BaseModel):
    campaign_id: str
    confidence: float = Field(ge=0, le=1)
    reasoning: str


class MailchimpMatchResult(BaseModel):
    candidates: list[MailchimpCampaignCandidate] = Field(default_factory=list)


class MailchimpCampaignMatcher:
    """Ranks Mailchimp campaign reports against a Cue event using the configured OpenAI model."""

    def __init__(self, organization):
        self.organization = organization

    def rank(self, event, reports: list[dict]) -> MailchimpMatchResult:
        if not reports:
            return MailchimpMatchResult()

        filtered_reports = self._prefilter_reports(event, reports)
        prompt = self._build_prompt(event, filtered_reports)

        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(
            model=getattr(settings, "OPENAI_MODEL", "gpt-4o"),
            api_key=getattr(settings, "OPENAI_API_KEY", ""),
            temperature=0.0,
        )
        structured_llm = llm.with_structured_output(MailchimpMatchResult)
        result = structured_llm.invoke([
            {
                "role": "system",
                "content": (
                    "Match Mailchimp email campaigns to an event. Rank by event name overlap, "
                    "subject/title hints, date proximity, venue/city hints, and engagement relevance. "
                    "Return up to 10 candidates sorted by confidence. Use confidence below 0.3 for weak matches."
                ),
            },
            {"role": "user", "content": prompt},
        ])
        if isinstance(result, MailchimpMatchResult):
            return self._limit_result(result)
        if hasattr(MailchimpMatchResult, "model_validate"):
            return self._limit_result(MailchimpMatchResult.model_validate(result))
        return self._limit_result(MailchimpMatchResult.parse_obj(result))

    def _prefilter_reports(self, event, reports: list[dict]) -> list[dict]:
        if len(reports) <= 50:
            return reports

        window_start = event.start_date - timedelta(days=90)
        window_end = event.start_date + timedelta(days=7)
        filtered = []
        for report in reports:
            sent_date = _parse_date(report.get("send_time"))
            if sent_date and window_start <= sent_date <= window_end:
                filtered.append(report)

        return filtered[:50] if filtered else reports[:50]

    def _build_prompt(self, event, reports: list[dict]) -> str:
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
        report_payload = [
            {
                "id": report.get("id"),
                "campaign_title": report.get("campaign_title"),
                "subject_line": report.get("subject_line"),
                "type": report.get("type"),
                "send_time": report.get("send_time"),
                "archive_url": report.get("archive_url"),
                "emails_sent": report.get("emails_sent"),
                "opens": report.get("opens"),
                "clicks": report.get("clicks"),
                "ecommerce": report.get("ecommerce"),
            }
            for report in reports
        ]
        return (
            "Event:\n"
            f"{json.dumps(event_payload, indent=2)}\n\n"
            "Mailchimp campaign reports:\n"
            f"{json.dumps(report_payload, indent=2, default=str)}"
        )

    @staticmethod
    def _limit_result(result: MailchimpMatchResult) -> MailchimpMatchResult:
        candidates = sorted(result.candidates, key=lambda item: item.confidence, reverse=True)[:10]
        return MailchimpMatchResult(candidates=candidates)


def _parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10]) if isinstance(value, str) else None
    except (TypeError, ValueError, IndexError):
        return None
