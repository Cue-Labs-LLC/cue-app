import json
from datetime import date

from django.conf import settings
from pydantic import BaseModel, Field

from tickets.models import AITokenUsage
from tickets.services.ai_metering import record_ai_token_usage


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
        max_candidates = getattr(settings, "MAILCHIMP_MATCH_MAX_CANDIDATES", 50)

        from langchain_openai import ChatOpenAI

        model_name = getattr(settings, "OPENAI_MODEL", "gpt-4o")
        llm = ChatOpenAI(
            model=model_name,
            api_key=getattr(settings, "OPENAI_API_KEY", ""),
            temperature=0.0,
            stream_usage=True,
        )
        structured_llm = llm.with_structured_output(MailchimpMatchResult, include_raw=True)
        raw_result = structured_llm.invoke([
            {"role": "system", "content": self._build_system_prompt(max_candidates)},
            {"role": "user", "content": prompt},
        ])

        if isinstance(raw_result, dict) and {'raw', 'parsed', 'parsing_error'} <= set(raw_result):
            record_ai_token_usage(
                organization=self.organization,
                feature=AITokenUsage.FEATURE_MAILCHIMP_CAMPAIGN_MATCH,
                model_name=model_name,
                usage=raw_result.get('raw'),
                metadata={
                    'event_id': str(event.id),
                    'campaign_count': len(filtered_reports),
                },
            )
            if raw_result.get('parsing_error'):
                raise raw_result['parsing_error']
            result = raw_result.get('parsed')
        else:
            result = raw_result

        if isinstance(result, MailchimpMatchResult):
            return self._limit_result(result)
        if hasattr(MailchimpMatchResult, "model_validate"):
            return self._limit_result(MailchimpMatchResult.model_validate(result))
        return self._limit_result(MailchimpMatchResult.parse_obj(result))

    def _prefilter_reports(self, event, reports: list[dict]) -> list[dict]:
        max_candidates = getattr(settings, "MAILCHIMP_MATCH_PREFILTER_MAX", 150)
        sent_reports = [r for r in reports if _parse_date(r.get("send_time")) is not None]
        if len(sent_reports) <= max_candidates:
            return sent_reports
        anchor = event.start_date
        if anchor is None:
            return sent_reports[:max_candidates]
        sent_reports.sort(key=lambda r: abs((_parse_date(r["send_time"]) - anchor).days))
        return sent_reports[:max_candidates]

    def _build_system_prompt(self, max_candidates: int) -> str:
        hints = (getattr(self.organization, "mailchimp_campaign_title_hints", "") or "").strip()
        hints_block = (
            "\n\nThe organization has provided these hints about their campaign "
            f"naming conventions:\n{hints}"
        ) if hints else ""
        return (
            "Match Mailchimp email campaigns to an event. Signals (in order of reliability):\n"
            "1. subject_line that mentions the event name, city, or date.\n"
            "2. send_time proximity to the event's start_date.\n"
            "3. list_name that matches the event's venue city or region.\n"
            "4. campaign_title - often a cryptic internal code. Look for short "
            "city abbreviations (e.g. 2-3 letter prefixes) and embedded dates "
            "(MMDDYYYY, YYYYMMDD, MMDDYY) that match the event.\n\n"
            "Confidence calibration:\n"
            "- HIGH (0.7-0.95): subject_line clearly references the event, OR "
            "campaign_title encodes the event's city AND date, OR list_name matches "
            "the event's city/region and send_time is within 90 days.\n"
            "- MEDIUM (0.4-0.7): plausible match on 1-2 signals.\n"
            "- LOW (below 0.3): no clear identification signal.\n\n"
            f"Return up to {max_candidates} candidates sorted by confidence."
            f"{hints_block}"
        )

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
                "send_time": report.get("send_time"),
                "list_name": report.get("list_name"),
            }
            for report in reports
        ]
        return (
            "Event:\n"
            f"{json.dumps(event_payload, indent=2)}\n\n"
            "Mailchimp campaign reports:\n"
            f"{json.dumps(report_payload, default=str)}"
        )

    @staticmethod
    def _limit_result(result: MailchimpMatchResult) -> MailchimpMatchResult:
        max_candidates = getattr(settings, "MAILCHIMP_MATCH_MAX_CANDIDATES", 50)
        candidates = sorted(result.candidates, key=lambda item: item.confidence, reverse=True)[:max_candidates]
        return MailchimpMatchResult(candidates=candidates)


def _parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10]) if isinstance(value, str) else None
    except (TypeError, ValueError, IndexError):
        return None
