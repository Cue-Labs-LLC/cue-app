"""ReAct answer agent for the Instagram DM support agent.

Mirrors the LangGraph ReAct pattern in ``tickets/services/chat/agent.py`` but with the
dedicated, customer-safe tool set (``build_ig_tools``) and no user (it answers the
public). The call is non-streaming: we ``.invoke`` once, then read the final reply,
which tools fired (for the groundedness gate), and token usage (for metering).
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from django.conf import settings

from ...models import AITokenUsage
from ..ai_metering import TokenUsageAccumulator, record_ai_token_usage
from .prompts import SYSTEM_PROMPT
from .tools import build_ig_tools

logger = logging.getLogger(__name__)

# How many prior messages of the conversation to feed the agent (D12 multi-turn).
HISTORY_LIMIT = 10


class InstagramAgentError(Exception):
    """Raised when the LLM can't be initialized or called."""


@dataclass
class AnswerResult:
    """Outcome of one answer pass."""

    text: str
    usage: object = None
    tool_calls: list = field(default_factory=list)

    @property
    def grounded(self) -> bool:
        """True when the answer was derived from at least one tool call (D14)."""
        return bool(self.tool_calls)


class InstagramSupportAgentService:
    """Generates a customer-facing reply for an Instagram DM, scoped to one org."""

    def __init__(self, organization):
        self.organization = organization

    def _build_agent(self):
        from langchain_openai import ChatOpenAI
        from langgraph.prebuilt import create_react_agent

        llm = ChatOpenAI(
            model=getattr(settings, 'OPENAI_MODEL', 'gpt-4o'),
            api_key=getattr(settings, 'OPENAI_API_KEY', ''),
            temperature=0.3,
            stream_usage=True,
        )
        return create_react_agent(llm, build_ig_tools(self.organization))

    def _history_messages(self, conversation):
        """Last-N prior messages as role/content dicts (oldest first)."""
        if conversation is None:
            return []
        from ...models import InstagramMessage

        rows = list(
            InstagramMessage.objects
            .filter(conversation=conversation)
            .order_by('-created_at')[:HISTORY_LIMIT]
        )
        rows.reverse()
        messages = []
        for row in rows:
            if not row.content:
                continue
            role = 'user' if row.author == 'customer' else 'assistant'
            messages.append({'role': role, 'content': row.content})
        return messages

    def answer(self, conversation, inbound_text) -> AnswerResult:
        """Draft a reply to ``inbound_text``. ``conversation`` may be None (shim/eval)."""
        try:
            agent = self._build_agent()
        except Exception as exc:
            logger.error("Failed to build Instagram support agent: %s", exc)
            raise InstagramAgentError(
                "The support agent is not available right now. Check that the OpenAI "
                "API key is configured and try again."
            ) from exc

        messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]
        messages.extend(self._history_messages(conversation))
        messages.append({'role': 'user', 'content': inbound_text})

        try:
            result = agent.invoke({'messages': messages})
        except Exception as exc:
            logger.error("Instagram support agent call failed: %s", exc)
            raise InstagramAgentError(
                "The support agent could not generate a reply. Please try again."
            ) from exc

        out_messages = result.get('messages', []) if isinstance(result, dict) else []

        accumulator = TokenUsageAccumulator()
        tool_calls = []
        final_text = ""
        for index, message in enumerate(out_messages):
            accumulator.add(message, key=str(index))
            for call in getattr(message, 'tool_calls', None) or []:
                name = call.get('name') if isinstance(call, dict) else getattr(call, 'name', None)
                if name:
                    tool_calls.append(name)
            content = getattr(message, 'content', None)
            if content:
                final_text = content if isinstance(content, str) else str(content)

        usage = accumulator.total()
        record_ai_token_usage(
            organization=self.organization,
            feature=AITokenUsage.FEATURE_IG_SUPPORT_AGENT,
            model_name=getattr(settings, 'OPENAI_MODEL', 'gpt-4o'),
            user=None,
            usage=usage,
            metadata={'stage': 'answer'},
        )

        return AnswerResult(text=final_text, usage=usage, tool_calls=tool_calls)
