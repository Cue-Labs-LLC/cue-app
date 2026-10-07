"""Escalation classifier + auto-send gate for the Instagram DM support agent.

A deterministic (temperature=0) structured pass that decides whether a drafted reply
can be auto-sent or must be queued for a human. Mirrors the ``with_structured_output(
..., include_raw=True)`` + metering pattern in ``sms_strategist.generate_campaign_plan``.
"""

import logging
from typing import Literal

from django.conf import settings
from pydantic import BaseModel, Field

from ...models import AITokenUsage
from ..ai_metering import record_ai_token_usage
from .agent import InstagramAgentError
from .prompts import CLASSIFIER_PROMPT

logger = logging.getLogger(__name__)

EscalationCategory = Literal[
    'routine', 'refund_dispute', 'complaint', 'partnership',
    'guest_list', 'safety', 'other',
]


class EscalationDecision(BaseModel):
    """Structured triage decision for one inbound DM."""

    should_escalate: bool = Field(
        description="True if a human must handle this message instead of auto-sending."
    )
    confidence: float = Field(
        default=0.0,
        description="0..1 certainty in this classification.",
    )
    category: EscalationCategory = Field(
        default='other',
        description="The triage category; 'routine' means a normal FAQ/event question.",
    )
    reason: str = Field(
        default='',
        description="One short sentence explaining the decision.",
    )


def classify_escalation(organization, question, draft_answer) -> EscalationDecision:
    """Classify whether ``question`` (and its drafted reply) needs a human.

    Raises InstagramAgentError if the LLM is unavailable or returns unreadable output.
    """
    from langchain_openai import ChatOpenAI

    model_name = getattr(settings, 'OPENAI_MODEL', 'gpt-4o')
    user_content = (
        f"Customer message:\n{question}\n\n"
        f"Assistant's drafted reply:\n{draft_answer}"
    )

    try:
        llm = ChatOpenAI(
            model=model_name,
            api_key=getattr(settings, 'OPENAI_API_KEY', ''),
            temperature=0,
            stream_usage=True,
        )
        structured_llm = llm.with_structured_output(EscalationDecision, include_raw=True)
        raw_result = structured_llm.invoke([
            {'role': 'system', 'content': CLASSIFIER_PROMPT},
            {'role': 'user', 'content': user_content},
        ])
    except Exception as exc:
        logger.error("Instagram escalation classifier LLM call failed: %s", exc)
        raise InstagramAgentError(
            "The support agent is not available right now. Check that the OpenAI API "
            "key is configured and try again."
        ) from exc

    if isinstance(raw_result, dict) and {'raw', 'parsed', 'parsing_error'} <= set(raw_result):
        record_ai_token_usage(
            organization=organization,
            feature=AITokenUsage.FEATURE_IG_SUPPORT_AGENT,
            model_name=model_name,
            user=None,
            usage=raw_result.get('raw'),
            metadata={'stage': 'classify'},
        )
        if raw_result.get('parsing_error'):
            raise InstagramAgentError("The agent returned an unreadable decision. Please try again.")
        result = raw_result.get('parsed')
    else:
        result = raw_result

    if not isinstance(result, EscalationDecision):
        result = EscalationDecision.model_validate(result)
    return result


def decide_autosend(decision, answer_result) -> bool:
    """Whether the drafted reply may be auto-sent (offline portion of the gate).

    Auto-send only when the classifier says routine + not-escalate, confidence clears
    the configured threshold, AND the answer is grounded in a tool call (D14 — a fluent
    answer with no tool hit is the hallucination shape, so it is queued). The daily cap
    (D4) and transport live in Phase 3; this helper intentionally omits them.
    """
    threshold = getattr(settings, 'IG_AGENT_AUTOSEND_MIN_CONFIDENCE', 0.8)
    return (
        not decision.should_escalate
        and decision.category == 'routine'
        and decision.confidence >= threshold
        and answer_result.grounded
    )
