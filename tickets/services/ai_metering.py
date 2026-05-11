"""Helpers for recording organization-scoped AI token usage."""

from dataclasses import dataclass
from datetime import datetime

from django.db.models import Sum
from django.utils import timezone

from tickets.models import AITokenUsage


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @property
    def has_usage(self):
        return self.prompt_tokens > 0 or self.completion_tokens > 0 or self.total_tokens > 0


class TokenUsageAccumulator:
    """Accumulates LangChain streaming usage without double-counting chunks."""

    def __init__(self):
        self._usage_by_key = {}

    def add(self, usage, key=None):
        token_usage = extract_token_usage(usage)
        if not token_usage.has_usage:
            return

        usage_key = key or 'default'
        existing = self._usage_by_key.get(usage_key, TokenUsage())
        self._usage_by_key[usage_key] = TokenUsage(
            prompt_tokens=max(existing.prompt_tokens, token_usage.prompt_tokens),
            completion_tokens=max(existing.completion_tokens, token_usage.completion_tokens),
            total_tokens=max(existing.total_tokens, token_usage.total_tokens),
        )

    def total(self):
        prompt_tokens = sum(usage.prompt_tokens for usage in self._usage_by_key.values())
        completion_tokens = sum(usage.completion_tokens for usage in self._usage_by_key.values())
        total_tokens = sum(
            max(usage.total_tokens, usage.prompt_tokens + usage.completion_tokens)
            for usage in self._usage_by_key.values()
        )
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )


def extract_token_usage(source):
    """Normalize LangChain/OpenAI usage metadata into TokenUsage."""
    usage = source
    if source is None:
        return TokenUsage()
    if isinstance(source, TokenUsage):
        return source

    if hasattr(source, 'usage_metadata'):
        usage = getattr(source, 'usage_metadata') or {}
    elif hasattr(source, 'response_metadata'):
        usage = (getattr(source, 'response_metadata') or {}).get('token_usage', {})

    if hasattr(usage, 'dict'):
        usage = usage.dict()
    elif hasattr(usage, 'model_dump'):
        usage = usage.model_dump()

    if not isinstance(usage, dict):
        return TokenUsage()

    nested_usage = usage.get('token_usage')
    if isinstance(nested_usage, dict):
        usage = nested_usage

    prompt_tokens = _as_int(
        usage.get('prompt_tokens')
        or usage.get('input_tokens')
        or usage.get('prompt_token_count')
    )
    completion_tokens = _as_int(
        usage.get('completion_tokens')
        or usage.get('output_tokens')
        or usage.get('completion_token_count')
    )
    total_tokens = _as_int(usage.get('total_tokens') or usage.get('total_token_count'))
    if not total_tokens:
        total_tokens = prompt_tokens + completion_tokens

    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


def usage_key_from_metadata(metadata):
    """Return a stable key for one LLM run in LangGraph/LangChain streams."""
    if not isinstance(metadata, dict):
        return 'default'

    for key in ('run_id', 'checkpoint_id', 'langgraph_checkpoint_ns'):
        if metadata.get(key):
            return str(metadata[key])

    node = metadata.get('langgraph_node') or 'default'
    step = metadata.get('langgraph_step')
    if step is not None:
        return f"{node}:{step}"
    return str(node)


def record_ai_token_usage(
    *,
    organization,
    feature,
    model_name='',
    user=None,
    usage=None,
    request_id='',
    metadata=None,
    occurred_at=None,
):
    """Create a billable usage record when token metadata is available."""
    token_usage = extract_token_usage(usage)
    if not token_usage.has_usage:
        return None

    return AITokenUsage.objects.create(
        organization=organization,
        user=user if getattr(user, 'is_authenticated', True) else None,
        feature=feature,
        model_name=model_name or '',
        prompt_tokens=token_usage.prompt_tokens,
        completion_tokens=token_usage.completion_tokens,
        total_tokens=token_usage.total_tokens,
        request_id=request_id or '',
        occurred_at=occurred_at or timezone.now(),
        metadata=metadata or {},
    )


def monthly_ai_token_usage(organization, year, month):
    """Aggregate billable token usage for an organization and calendar month."""
    start = datetime(year, month, 1, tzinfo=timezone.get_current_timezone())
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.get_current_timezone())
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.get_current_timezone())

    totals = (
        AITokenUsage.objects
        .filter(organization=organization, occurred_at__gte=start, occurred_at__lt=end)
        .aggregate(
            prompt_tokens=Sum('prompt_tokens'),
            completion_tokens=Sum('completion_tokens'),
            total_tokens=Sum('total_tokens'),
        )
    )
    return {
        'prompt_tokens': totals['prompt_tokens'] or 0,
        'completion_tokens': totals['completion_tokens'] or 0,
        'total_tokens': totals['total_tokens'] or 0,
    }


def _as_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
