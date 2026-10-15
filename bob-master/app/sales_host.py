"""
Host-based routing for the dedicated sales dashboard hostname (2026-10-15).

sales.advancedmarketers.co points at this same bob-api deployment (one Railway
service, many hostnames), so without a guard EVERY route -- Pulse, admin pages,
the task-trigger POSTs -- would also answer on that name. This middleware makes
the sales host a narrow view of the app:

  - "/" is served as the sales audit dashboard (internal path rewrite; the URL
    in the browser stays "/").
  - only the sales-audit routes and /health are reachable; anything else is 404.
  - requests for any OTHER hostname (the Railway URL, localhost, tests) pass
    through completely untouched.

Pure ASGI (not BaseHTTPMiddleware) so it can rewrite the path cheaply and never
interferes with the app's background-job threads. The host comes from
Settings.sales_dashboard_host (empty = feature off).
"""
from __future__ import annotations

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings

_ROOT_TARGET = "/reports/sales-audit"
_ALLOWED_PREFIXES = ("/reports/sales-audit",)
_ALLOWED_EXACT = ("/health",)


def _request_host(scope: Scope) -> str:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    # Cloudflare/Railway both preserve the original Host; x-forwarded-host is a fallback if a proxy rewrote it.
    raw = headers.get("x-forwarded-host") or headers.get("host") or ""
    return raw.split(",")[0].strip().split(":")[0].lower()


class SalesHostMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        sales_host = (get_settings().sales_dashboard_host or "").strip().lower()
        if not sales_host or _request_host(scope) != sales_host:
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        if path == "/":
            scope = {**scope, "path": _ROOT_TARGET, "raw_path": _ROOT_TARGET.encode()}
        elif not (path in _ALLOWED_EXACT or any(path == p or path.startswith(p + "/") for p in _ALLOWED_PREFIXES)):
            await PlainTextResponse("Not found", status_code=404)(scope, receive, send)
            return
        await self.app(scope, receive, send)
