"""Langfuse tracing for production LangChain / LangGraph runs.

This is the single, self-host-ready LLM observability standard for the app (see the ADR
in ``docs/adr/0001-llm-observability-langfuse.md``). It replaces the previous env-only
LangSmith tracing so there is one governed place prompts + tool results are sent, and so
that destination can be a self-hosted Langfuse inside our own boundary (set ``LANGFUSE_HOST``)
rather than a third-party cloud — important when traces contain customer PII (e.g. IG DMs).

It no-ops unless ``LANGFUSE_PUBLIC_KEY`` + ``LANGFUSE_SECRET_KEY`` are configured, so it is
safe to leave wired into every call site in every environment. Call ``trace_config(...)`` and
spread the result into a LangChain ``.invoke`` / ``.stream``::

    agent.invoke({"messages": msgs}, **trace_config(name="ig-answer", session_id=conv.id))

When tracing is off this returns ``{}`` and the call is unchanged.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# Lazily initialize the Langfuse client once per process. ``None`` = not yet checked.
_enabled = None


def _tracing_enabled() -> bool:
    """True if Langfuse is configured; initializes the client singleton on first use."""
    global _enabled
    if _enabled is not None:
        return _enabled
    if not (getattr(settings, 'LANGFUSE_PUBLIC_KEY', '') and
            getattr(settings, 'LANGFUSE_SECRET_KEY', '')):
        _enabled = False
        return _enabled
    try:
        from langfuse import Langfuse

        # Registers the process-wide client the CallbackHandler picks up. ``host`` lets us
        # point at a self-hosted instance so customer data never leaves our boundary.
        Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            host=getattr(settings, 'LANGFUSE_HOST', None),
        )
        _enabled = True
    except Exception:
        logger.exception("Langfuse init failed; LLM tracing disabled")
        _enabled = False
    return _enabled


def trace_config(*, name=None, tags=None, session_id=None, user_id=None, metadata=None):
    """Return a LangChain ``config`` kwarg routing the run to Langfuse, or ``{}`` if off.

    Spread into a Runnable call: ``llm.invoke(x, **trace_config(...))``. ``session_id`` groups
    related traces (e.g. all LLM calls for one conversation); ``tags``/``metadata`` are for
    filtering in the Langfuse UI.
    """
    if not _tracing_enabled():
        return {}
    try:
        from langfuse.langchain import CallbackHandler

        handler = CallbackHandler()
    except Exception:
        logger.exception("Langfuse CallbackHandler unavailable; LLM tracing disabled")
        return {}

    md = dict(metadata or {})
    if session_id:
        md['langfuse_session_id'] = str(session_id)
    if user_id:
        md['langfuse_user_id'] = str(user_id)
    if tags:
        md['langfuse_tags'] = list(tags)

    config = {'callbacks': [handler]}
    if name:
        config['run_name'] = name
    if md:
        config['metadata'] = md
    return {'config': config}
