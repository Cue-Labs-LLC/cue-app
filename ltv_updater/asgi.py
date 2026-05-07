import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ltv_updater.settings")

import django
django.setup()

import anyio
from django.core.asgi import get_asgi_application
from tickets.mcp_app import build_mcp_app

_django_asgi = get_asgi_application()
_mcp_app = build_mcp_app()


async def application(scope, receive, send):
    if scope["type"] == "lifespan":
        await _lifespan(scope, receive, send)
        return
    path = scope.get("path", "")
    if path == "/mcp" or path.startswith("/mcp/"):
        await _mcp_app(scope, receive, send)
    else:
        await _django_asgi(scope, receive, send)


async def _lifespan(scope, receive, send):
    """Multiplex ASGI lifespan to both Django and FastMCP so each can initialize."""
    shutdown = anyio.Event()
    django_up, mcp_up = anyio.Event(), anyio.Event()
    django_down, mcp_down = anyio.Event(), anyio.Event()

    def _recv():
        started = [False]

        async def recv():
            if not started[0]:
                started[0] = True
                return {"type": "lifespan.startup"}
            await shutdown.wait()
            return {"type": "lifespan.shutdown"}

        return recv

    def _send(up, down):
        async def send_fn(msg):
            t = msg.get("type", "")
            if t == "lifespan.startup.complete":
                up.set()
            elif t == "lifespan.shutdown.complete":
                down.set()

        return send_fn

    async with anyio.create_task_group() as tg:
        tg.start_soon(_django_asgi, scope, _recv(), _send(django_up, django_down))
        tg.start_soon(_mcp_app, scope, _recv(), _send(mcp_up, mcp_down))
        await django_up.wait()
        await mcp_up.wait()
        await send({"type": "lifespan.startup.complete"})
        await receive()  # lifespan.shutdown from server
        shutdown.set()
        await django_down.wait()
        await mcp_down.wait()
        await send({"type": "lifespan.shutdown.complete"})
