import asyncio
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ltv_updater.settings")

import django
django.setup()

from django.core.asgi import get_asgi_application
from tickets.mcp_app import build_mcp_app, mcp as fastmcp_instance

_django_asgi = get_asgi_application()
_mcp_app = build_mcp_app()

_mcp_ready: asyncio.Event | None = None
_mcp_shutdown: asyncio.Event | None = None


async def _run_session_manager(ready: asyncio.Event, shutdown: asyncio.Event) -> None:
    async with fastmcp_instance.session_manager.run():
        ready.set()
        await shutdown.wait()


async def application(scope, receive, send):
    global _mcp_ready, _mcp_shutdown
    if scope["type"] == "lifespan":
        if _mcp_ready is None:
            _mcp_ready = asyncio.Event()
            _mcp_shutdown = asyncio.Event()
            asyncio.create_task(_run_session_manager(_mcp_ready, _mcp_shutdown))
            await _mcp_ready.wait()
        try:
            await _django_asgi(scope, receive, send)
        finally:
            if _mcp_shutdown is not None:
                _mcp_shutdown.set()
        return
    path = scope.get("path", "")
    if path == "/mcp" or path.startswith("/mcp/"):
        await _mcp_app(scope, receive, send)
    else:
        await _django_asgi(scope, receive, send)
