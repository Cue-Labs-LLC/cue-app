import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ltv_updater.settings")

import django
django.setup()

from django.core.asgi import get_asgi_application
from tickets.mcp_app import build_mcp_app

_django_asgi = get_asgi_application()
_mcp_app = build_mcp_app()


async def application(scope, receive, send):
    path = scope.get("path", "")
    if path == "/mcp" or path.startswith("/mcp/"):
        await _mcp_app(scope, receive, send)
    else:
        await _django_asgi(scope, receive, send)
