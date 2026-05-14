"""On-demand AI narrative for the marketing overview page."""

import json
import logging
from decimal import Decimal
from typing import Literal

from django.conf import settings
from pydantic import BaseModel, Field

from tickets.models import AITokenUsage
from tickets.services.ai_metering import record_ai_token_usage


logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a marketing analyst for an event-ticketing platform. Given an organization's "
    "channel metrics for a recent window, produce a short, plainspoken narrative the organizer "
    "can act on. Keep insights concrete: name the channel, the number, and the next step. "
    "Avoid hedging. No emojis."
)


class Insight(BaseModel):
    title: str = Field(description="Short headline for this insight (max 80 chars).")
    body: str = Field(description="1-2 sentence narrative explaining the insight with the relevant numbers.")
    severity: Literal['info', 'warning', 'critical'] = Field(description="Visual urgency tier.")
    recommended_action: str = Field(description="One concrete next step the organizer can take.")


class MarketingNarrativeResult(BaseModel):
    headline: str = Field(description="One-sentence overall takeaway for the window.")
    insights: list[Insight] = Field(description="3 to 5 ordered insights, most important first.")


def _serialize_for_prompt(metrics: dict) -> dict:
    """Convert Decimal/datetime in metrics into JSON-safe primitives, drop heavy fields."""

    def coerce(value):
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, dict):
            return {k: coerce(v) for k, v in value.items()}
        if isinstance(value, list):
            return [coerce(v) for v in value]
        return value

    trimmed = {
        'channels': coerce(metrics.get('channels', {})),
        'channel_comparison': coerce(metrics.get('channel_comparison', [])),
        'trends': coerce(metrics.get('trends', [])[-12:]),
        'top_campaigns': coerce(metrics.get('top_campaigns', [])[:5]),
        'top_events_by_roi': coerce(metrics.get('top_events_by_roi', [])[:5]),
        'meta': metrics.get('meta', {}),
    }
    return trimmed


def generate_marketing_narrative(organization, metrics: dict, window_label: str) -> dict:
    """Ask the configured OpenAI model for 3-5 marketing insights about the metrics."""
    from langchain_openai import ChatOpenAI

    model_name = getattr(settings, 'OPENAI_MODEL', 'gpt-4o')
    llm = ChatOpenAI(
        model=model_name,
        api_key=getattr(settings, 'OPENAI_API_KEY', ''),
        temperature=0.2,
        stream_usage=True,
    )
    structured_llm = llm.with_structured_output(MarketingNarrativeResult, include_raw=True)

    prompt_payload = _serialize_for_prompt(metrics)
    user_content = (
        f"Window: {window_label}\n"
        f"Metrics JSON follows. Use only what's present — don't invent numbers.\n\n"
        f"{json.dumps(prompt_payload, default=str)}"
    )

    raw_result = structured_llm.invoke([
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': user_content},
    ])

    if isinstance(raw_result, dict) and {'raw', 'parsed', 'parsing_error'} <= set(raw_result):
        record_ai_token_usage(
            organization=organization,
            feature=AITokenUsage.FEATURE_MARKETING_NARRATIVE,
            model_name=model_name,
            usage=raw_result.get('raw'),
            metadata={'window_label': window_label},
        )
        if raw_result.get('parsing_error'):
            raise raw_result['parsing_error']
        result = raw_result.get('parsed')
    else:
        result = raw_result

    if not isinstance(result, MarketingNarrativeResult):
        if hasattr(MarketingNarrativeResult, 'model_validate'):
            result = MarketingNarrativeResult.model_validate(result)
        else:
            result = MarketingNarrativeResult.parse_obj(result)

    return {
        'headline': result.headline,
        'insights': [insight.model_dump() for insight in result.insights],
    }
