"""
ChatAgentService — builds a LangGraph ReAct agent, streams responses via SSE,
and persists messages to the database.
"""

import json
import logging
import uuid

from django.conf import settings

from tickets.models import AITokenUsage, ChatMessage
from tickets.services.ai_metering import (
    TokenUsageAccumulator,
    record_ai_token_usage,
    usage_key_from_metadata,
)

from .history import load_history
from .prompts import SYSTEM_PROMPT
from .tools import build_tools

logger = logging.getLogger(__name__)


class ChatAgentService:
    """Manages a conversational agent scoped to an organization."""

    def __init__(self, organization, user):
        self.organization = organization
        self.user = user

    def _build_agent(self):
        """Construct the LangGraph ReAct agent with org-scoped tools."""
        from langchain_openai import ChatOpenAI
        from langgraph.prebuilt import create_react_agent

        llm = ChatOpenAI(
            model=getattr(settings, 'OPENAI_MODEL', 'gpt-4o'),
            api_key=getattr(settings, 'OPENAI_API_KEY', ''),
            temperature=0.3,
            streaming=True,
            stream_usage=True,
        )
        tools = build_tools(self.organization)
        agent = create_react_agent(llm, tools)
        return agent

    def stream_response(self, user_message, conversation_id):
        """
        Generator that yields SSE-formatted chunks.

        Persists the user message before streaming and the full assistant
        response after streaming completes.
        """
        # Persist user message
        ChatMessage.objects.create(
            organization=self.organization,
            user=self.user,
            conversation_id=conversation_id,
            role='user',
            content=user_message,
        )

        # Load history
        history = load_history(self.organization, self.user, conversation_id)

        # Build messages list for the agent
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        # Add history (excluding the just-saved user message, since we add it below)
        for msg in history[:-1]:
            messages.append(msg)
        messages.append({"role": "user", "content": user_message})

        try:
            agent = self._build_agent()
        except Exception as e:
            logger.error("Failed to build chat agent: %s", e)
            error_msg = "I'm sorry, the chat service is not available right now. Please check that the OpenAI API key is configured."
            yield f"data: {json.dumps({'type': 'token', 'content': error_msg})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            ChatMessage.objects.create(
                organization=self.organization,
                user=self.user,
                conversation_id=conversation_id,
                role='assistant',
                content=error_msg,
            )
            return

        full_response = ""
        usage_accumulator = TokenUsageAccumulator()
        try:
            for event in agent.stream(
                {"messages": messages},
                stream_mode="messages",
            ):
                # event is a tuple of (message, metadata)
                msg, metadata = event
                metadata = metadata or {}
                if metadata.get('langgraph_node') == 'agent':
                    usage_accumulator.add(msg, key=usage_key_from_metadata(metadata))
                # We only want to stream AI message content tokens
                if hasattr(msg, 'content') and msg.content and metadata.get('langgraph_node') == 'agent':
                    token = msg.content
                    full_response += token
                    yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

        except Exception as e:
            logger.error("Chat agent streaming error: %s", e)
            error_msg = "\n\n[An error occurred while generating the response.]"
            full_response += error_msg
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

        # Signal done
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

        # Persist assistant response
        if full_response.strip():
            token_usage = usage_accumulator.total()
            ChatMessage.objects.create(
                organization=self.organization,
                user=self.user,
                conversation_id=conversation_id,
                role='assistant',
                content=full_response,
                token_count=token_usage.total_tokens,
            )
            record_ai_token_usage(
                organization=self.organization,
                feature=AITokenUsage.FEATURE_CHAT_AGENT,
                model_name=getattr(settings, 'OPENAI_MODEL', 'gpt-4o'),
                user=self.user,
                usage=token_usage,
                metadata={'conversation_id': str(conversation_id)},
            )
