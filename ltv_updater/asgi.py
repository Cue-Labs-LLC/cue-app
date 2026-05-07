import asyncio
import logging
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ltv_updater.settings")

import django
django.setup()

from django.core.asgi import get_asgi_application
from tickets.mcp_app import build_mcp_app, mcp as fastmcp_instance

logger = logging.getLogger(__name__)

_django_asgi = get_asgi_application()
_mcp_app = build_mcp_app()

_mcp_ready: asyncio.Event | None = None
_mcp_shutdown: asyncio.Event | None = None

logger.warning(
    "asgi.py module loaded: session_manager id=%s _has_started=%s _task_group=%s",
    id(fastmcp_instance.session_manager),
    fastmcp_instance.session_manager._has_started,
    fastmcp_instance.session_manager._task_group,
)


async def _run_session_manager(ready: asyncio.Event, shutdown: asyncio.Event) -> None:
    logger.warning("_run_session_manager: entering session_manager.run()")
    try:
        async with fastmcp_instance.session_manager.run():
            logger.warning(
                "_run_session_manager: run() entered, _task_group=%s",
                fastmcp_instance.session_manager._task_group,
            )
            ready.set()
            await shutdown.wait()
        logger.warning("_run_session_manager: run() context exited normally")
    except Exception:
        logger.exception("_run_session_manager: run() raised an exception")
        ready.set()  # unblock lifespan so uvicorn doesn't hang


async def application(scope, receive, send):
    global _mcp_ready, _mcp_shutdown
    if scope["type"] == "lifespan":
        logger.warning(
            "lifespan: startup — _mcp_ready=%s session_manager id=%s _task_group=%s",
            _mcp_ready,
            id(fastmcp_instance.session_manager),
            fastmcp_instance.session_manager._task_group,
        )
        if _mcp_ready is None:
            _mcp_ready = asyncio.Event()
            _mcp_shutdown = asyncio.Event()
            asyncio.create_task(_run_session_manager(_mcp_ready, _mcp_shutdown))
            await _mcp_ready.wait()
            logger.warning(
                "lifespan: _mcp_ready fired — _task_group=%s",
                fastmcp_instance.session_manager._task_group,
            )
        try:
            await _django_asgi(scope, receive, send)
        finally:
            logger.warning("lifespan: django lifespan done, setting _mcp_shutdown")
            if _mcp_shutdown is not None:
                _mcp_shutdown.set()
        return
    path = scope.get("path", "")
    if path == "/mcp" or path.startswith("/mcp/"):
        logger.warning(
            "mcp request: path=%s _task_group=%s session_manager id=%s",
            path,
            fastmcp_instance.session_manager._task_group,
            id(fastmcp_instance.session_manager),
        )
        await _mcp_app(scope, receive, send)
    else:
        await _django_asgi(scope, receive, send)
