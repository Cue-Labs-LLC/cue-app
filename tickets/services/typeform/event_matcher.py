"""Use the configured OpenAI model to suggest which event a survey response belongs to."""

import json
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from pydantic import BaseModel, Field

from tickets.models import AITokenUsage
from tickets.services.ai_metering import record_ai_token_usage


logger = logging.getLogger(__name__)


CONFIDENCE_THRESHOLD = Decimal('0.30')
EVENT_WINDOW_DAYS = 60
MAX_CANDIDATES = 3
MAX_EVENTS_IN_PROMPT = 40
MAX_RESPONSES_IN_PROMPT = 30
MAX_RESPONSE_CANDIDATES = 10


class EventCandidate(BaseModel):
    event_id: str
    confidence: float = Field(ge=0, le=1)
    reasoning: str


class MatchResult(BaseModel):
    candidates: list[EventCandidate] = Field(default_factory=list)


class SurveyEventMatcher:
    """Ranks recent organization events against a survey response's content + date."""

    def __init__(self, organization):
        self.organization = organization

    def suggest(self, response, events=None) -> MatchResult:
        from tickets.models import Event

        if events is None:
            window_start = response.responded_at - timedelta(days=EVENT_WINDOW_DAYS)
            window_end = response.responded_at + timedelta(days=EVENT_WINDOW_DAYS)
            events = (
                Event.objects.filter(
                    organization=self.organization,
                    start_date__range=(window_start, window_end),
                )
                .select_related('venue')
                .order_by('-start_date')[:MAX_EVENTS_IN_PROMPT]
            )

        events = list(events)
        if not events:
            return MatchResult()

        prompt = self._build_prompt(response, events)

        from langchain_openai import ChatOpenAI

        model_name = getattr(settings, 'OPENAI_MODEL', 'gpt-4o')
        llm = ChatOpenAI(
            model=model_name,
            api_key=getattr(settings, 'OPENAI_API_KEY', ''),
            temperature=0.0,
            stream_usage=True,
        )
        structured_llm = llm.with_structured_output(MatchResult, include_raw=True)
        raw_result = structured_llm.invoke([
            {
                'role': 'system',
                'content': (
                    'Match a post-event survey response to the most likely event the '
                    'respondent attended. Use response date proximity, city, venue or '
                    'artist mentions in feedback text, and any other contextual hints. '
                    'Return up to 3 candidates sorted by confidence. Use confidence below '
                    '0.3 for weak matches.'
                ),
            },
            {'role': 'user', 'content': prompt},
        ])

        if isinstance(raw_result, dict) and {'raw', 'parsed', 'parsing_error'} <= set(raw_result):
            record_ai_token_usage(
                organization=self.organization,
                feature=AITokenUsage.FEATURE_TYPEFORM_EVENT_MATCH,
                model_name=model_name,
                usage=raw_result.get('raw'),
                metadata={
                    'response_id': str(response.id),
                    'event_count': len(events),
                },
            )
            if raw_result.get('parsing_error'):
                raise raw_result['parsing_error']
            result = raw_result.get('parsed')
        else:
            result = raw_result

        if isinstance(result, MatchResult):
            return self._limit_result(result)
        if hasattr(MatchResult, 'model_validate'):
            return self._limit_result(MatchResult.model_validate(result))
        return self._limit_result(MatchResult.parse_obj(result))

    def _build_prompt(self, response, events) -> str:
        response_payload = {
            'responded_at': response.responded_at.isoformat() if response.responded_at else None,
            'answers': response.raw_answers or [],
        }
        event_payload = [
            {
                'id': str(event.id),
                'name': event.name,
                'summary': getattr(event, 'summary', ''),
                'start_date': event.start_date.isoformat() if event.start_date else None,
                'end_date': event.end_date.isoformat() if event.end_date else None,
                'venue': {
                    'name': getattr(event.venue, 'name', '') if event.venue_id else '',
                    'city': getattr(event.venue, 'city', '') if event.venue_id else '',
                    'state': getattr(event.venue, 'state', '') if event.venue_id else '',
                },
            }
            for event in events
        ]
        return (
            'Survey response:\n'
            f"{json.dumps(response_payload, indent=2, default=str)}\n\n"
            'Candidate events:\n'
            f"{json.dumps(event_payload, indent=2, default=str)}"
        )

    @staticmethod
    def _limit_result(result: MatchResult) -> MatchResult:
        candidates = sorted(
            result.candidates, key=lambda item: item.confidence, reverse=True,
        )[:MAX_CANDIDATES]
        return MatchResult(candidates=candidates)


class ResponseCandidate(BaseModel):
    response_id: str
    confidence: float = Field(ge=0, le=1)
    reasoning: str


class ResponseRankResult(BaseModel):
    candidates: list[ResponseCandidate] = Field(default_factory=list)


class EventSurveyMatcher:
    """Inverse of SurveyEventMatcher: ranks N ExternalSurveyResponse rows against ONE event."""

    def __init__(self, organization):
        self.organization = organization

    def rank(self, event, responses) -> ResponseRankResult:
        responses = list(responses)[:MAX_RESPONSES_IN_PROMPT]
        if not responses:
            return ResponseRankResult()

        prompt = self._build_prompt(event, responses)

        from langchain_openai import ChatOpenAI

        model_name = getattr(settings, 'OPENAI_MODEL', 'gpt-4o')
        llm = ChatOpenAI(
            model=model_name,
            api_key=getattr(settings, 'OPENAI_API_KEY', ''),
            temperature=0.0,
            stream_usage=True,
        )
        structured_llm = llm.with_structured_output(ResponseRankResult, include_raw=True)
        raw_result = structured_llm.invoke([
            {
                'role': 'system',
                'content': (
                    'Rank post-event survey responses by how likely each one is to belong '
                    'to the given event. Use response date proximity to the event, city or '
                    'venue mentions in the answers, and any other contextual hints. Return '
                    'up to 10 candidates sorted by confidence. Use confidence below 0.3 for '
                    'weak matches; omit responses you are confident do not belong.'
                ),
            },
            {'role': 'user', 'content': prompt},
        ])

        if isinstance(raw_result, dict) and {'raw', 'parsed', 'parsing_error'} <= set(raw_result):
            record_ai_token_usage(
                organization=self.organization,
                feature=AITokenUsage.FEATURE_TYPEFORM_EVENT_MATCH,
                model_name=model_name,
                usage=raw_result.get('raw'),
                metadata={
                    'event_id': str(event.id),
                    'response_count': len(responses),
                    'direction': 'event_to_responses',
                },
            )
            if raw_result.get('parsing_error'):
                raise raw_result['parsing_error']
            result = raw_result.get('parsed')
        else:
            result = raw_result

        if isinstance(result, ResponseRankResult):
            return self._limit_result(result)
        if hasattr(ResponseRankResult, 'model_validate'):
            return self._limit_result(ResponseRankResult.model_validate(result))
        return self._limit_result(ResponseRankResult.parse_obj(result))

    def _build_prompt(self, event, responses) -> str:
        event_payload = {
            'id': str(event.id),
            'name': event.name,
            'summary': getattr(event, 'summary', ''),
            'start_date': event.start_date.isoformat() if event.start_date else None,
            'end_date': event.end_date.isoformat() if event.end_date else None,
            'venue': {
                'name': getattr(event.venue, 'name', '') if event.venue_id else '',
                'city': getattr(event.venue, 'city', '') if event.venue_id else '',
                'state': getattr(event.venue, 'state', '') if event.venue_id else '',
            },
        }
        response_payload = [
            {
                'id': str(r.id),
                'responded_at': r.responded_at.isoformat() if r.responded_at else None,
                'answers': r.raw_answers or [],
            }
            for r in responses
        ]
        return (
            'Event:\n'
            f"{json.dumps(event_payload, indent=2, default=str)}\n\n"
            'Candidate survey responses:\n'
            f"{json.dumps(response_payload, indent=2, default=str)}"
        )

    @staticmethod
    def _limit_result(result: ResponseRankResult) -> ResponseRankResult:
        candidates = sorted(
            result.candidates, key=lambda item: item.confidence, reverse=True,
        )[:MAX_RESPONSE_CANDIDATES]
        return ResponseRankResult(candidates=candidates)


def apply_top_candidate(response, match: MatchResult) -> bool:
    """Write the top candidate (≥ CONFIDENCE_THRESHOLD) to the response. Returns True if saved."""
    if not match.candidates:
        return False
    top = match.candidates[0]
    confidence = Decimal(str(top.confidence))
    response.match_confidence = confidence
    response.match_reasoning = top.reasoning
    if confidence >= CONFIDENCE_THRESHOLD:
        from tickets.models import Event
        try:
            event = Event.objects.get(
                id=top.event_id, organization_id=response.organization_id,
            )
        except (Event.DoesNotExist, ValueError):
            event = None
        response.suggested_event = event
    else:
        response.suggested_event = None
    response.save(update_fields=['suggested_event', 'match_confidence', 'match_reasoning'])
    return True
