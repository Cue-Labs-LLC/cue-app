"""Chat history loader — retrieves recent messages for LangChain context."""

from django.conf import settings

from tickets.models import ChatMessage


def load_history(organization, user, conversation_id):
    """Return the last N ChatMessages for a conversation as LangChain message dicts."""
    max_messages = getattr(settings, 'CHAT_MAX_HISTORY', 50)
    messages = ChatMessage.objects.filter(
        organization=organization,
        user=user,
        conversation_id=conversation_id,
    ).order_by('created_at')[:max_messages]

    result = []
    for msg in messages:
        if msg.role == 'user':
            result.append({"role": "user", "content": msg.content})
        elif msg.role == 'assistant':
            result.append({"role": "assistant", "content": msg.content})
    return result
