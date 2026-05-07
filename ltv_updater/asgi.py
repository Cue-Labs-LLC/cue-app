import contextlib
import logging
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ltv_updater.settings")

import django
django.setup()

from django.core.asgi import get_asgi_application
from starlette.applications import Starlette

from tickets.mcp_app import build_mcp_app, mcp as fastmcp_instance

logger = logging.getLogger(__name__)

_django_asgi = get_asgi_application()
_mcp_app = build_mcp_app()


@contextlib.asynccontextmanager
async def lifespan(app):
    async with fastmcp_instance.session_manager.run():
        logger.info(
            "MCP session_manager.run() entered, _task_group=%s",
            fastmcp_instance.session_manager._task_group,
        )
        yield
    logger.info("MCP session_manager.run() exited")


_lifespan_app = Starlette(lifespan=lifespan)


async def application(scope, receive, send):
    if scope["type"] == "lifespan":
        await _lifespan_app(scope, receive, send)
        return
    path = scope.get("path", "")
    if path == "/mcp" or path.startswith("/mcp/"):
        await _mcp_app(scope, receive, send)
        return
    await _django_asgi(scope, receive, send)
