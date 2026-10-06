"""FAQ answer service for the Instagram DM support agent.

Mirrors the structured one-shot pattern in tickets/services/sms_strategist.py:
build model -> with_structured_output(Schema, include_raw=True) -> invoke -> meter.
No ReAct/tools loop is needed: the org's published FAQs fit directly in the prompt.
"""

import json
import logging
from typing import Optional

from django.conf import settings
from pydantic import BaseModel, Field

from ...models import AITokenUsage, OrgFAQ
from ..ai_metering import record_ai_token_usage

logger = logging.getLogger(__name__)


class InstagramAgentError(Exception):
    """Raised when the LLM can't be initialized or called."""


class FaqAnswer(BaseModel):
    """Structured result of a FAQ answer attempt."""

    answered: bool = Field(
        description="True only if a provided FAQ clearly answers the customer's message."
    )
    answer: str = Field(
        description="The reply to send. When answered is false, a short message saying a "
                    "team member will follow up."
    )
    matched_faq_id: Optional[str] = Field(
        default=None,
        description="The id of the FAQ the answer is based on, or null when answered is false.",
    )
    confidence: float = Field(
        default=0.0,
        description="0..1 certainty that the matched FAQ truly answers the question.",
    )


def _published_faqs(organization):
    """The FAQs the agent may answer from: published, org-scoped."""
    return (
        OrgFAQ.objects
        .filter(
            organization=organization,
            is_published=True,
            deleted_at__isnull=True,
        )
        .order_by('sort_order', 'created_at')
    )


def answer_faq(organization, *, question, user=None) -> FaqAnswer:
    """Answer a customer DM from the org's published FAQs.

    Raises InstagramAgentError if the LLM is unavailable. Returns a FaqAnswer; when no
    FAQ applies (or the question is sensitive), answered is False.
    """
    from langchain_openai import ChatOpenAI
    from .prompts import SYSTEM_PROMPT

    model_name = getattr(settings, 'OPENAI_MODEL', 'gpt-4o')

    faqs = [
        {'id': str(f.id), 'question': f.question, 'answer': f.answer, 'topic': f.topic}
        for f in _published_faqs(organization)
    ]
    user_content = (
        "Customer message:\n"
        f"{question}\n\n"
        "FAQs (answer only from these):\n"
        f"{json.dumps(faqs, ensure_ascii=False)}"
    )

    try:
        llm = ChatOpenAI(
            model=model_name,
            api_key=getattr(settings, 'OPENAI_API_KEY', ''),
            temperature=0.3,
            stream_usage=True,
        )
        structured_llm = llm.with_structured_output(FaqAnswer, include_raw=True)
        raw_result = structured_llm.invoke([
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': user_content},
        ])
    except Exception as exc:
        logger.error("Instagram FAQ agent LLM call failed: %s", exc)
        raise InstagramAgentError(
            "The support agent is not available right now. Check that the OpenAI API "
            "key is configured and try again."
        ) from exc

    if isinstance(raw_result, dict) and {'raw', 'parsed', 'parsing_error'} <= set(raw_result):
        record_ai_token_usage(
            organization=organization,
            feature=AITokenUsage.FEATURE_IG_SUPPORT_AGENT,
            model_name=model_name,
            user=user,
            usage=raw_result.get('raw'),
            metadata={'stage': 'answer'},
        )
        if raw_result.get('parsing_error'):
            raise InstagramAgentError("The agent returned an unreadable answer. Please try again.")
        result = raw_result.get('parsed')
    else:
        result = raw_result

    if not isinstance(result, FaqAnswer):
        result = FaqAnswer.model_validate(result)
    return result
