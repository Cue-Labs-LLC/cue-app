import contextlib
import logging
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ltv_updater.settings")

import django
django.setup()

from django.core.asgi import get_asgi_application
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

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


async def _django_catchall(scope: Scope, receive: Receive, send: Send) -> None:
    await _django_asgi(scope, receive, send)


application = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/mcp", _mcp_app, methods=["GET", "POST", "DELETE", "OPTIONS"]),
        Mount("/", app=_django_catchall),
    ],
)
