"""
Access-password gate for the sales dashboard (2026-10-15, Bob: "set an access password gate").

The password lives ONLY in the Railway env (SALES_DASHBOARD_PASSWORD) -- never in code, git, .env or docs. Empty/unset
= gate OFF (local dev and tests); main.py logs a loud warning at startup if it is off.

Mechanism: a login form sets a signed, expiring, HttpOnly cookie; scripts can send the password in an
`X-Sales-Password` header instead. The cookie is HMAC-signed with a key derived FROM the password, so changing the
password in Railway invalidates every existing session. Failed logins are rate-limited per client IP.

Scope: every route under /reports/sales-audit (the page, its JSON, run/push/override POSTs, on the Railway hostname
AND sales.advancedmarketers.co, where "/" is rewritten to it first). Left open on purpose: the login/logout routes and
the brand images (the login page itself shows the wordmark). Everything else in the app is untouched.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time
from collections import defaultdict, deque
from http.cookies import SimpleCookie
from urllib.parse import quote

from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings

BASE = "/reports/sales-audit"
LOGIN_PATH = f"{BASE}/login"
LOGOUT_PATH = f"{BASE}/logout"
COOKIE = "sales_audit_session"
SESSION_SECONDS = 30 * 24 * 3600

MAX_FAILURES = 8            # per IP ...
FAILURE_WINDOW = 10 * 60    # ... per 10 minutes, then 429


def configured_password() -> str:
    return (get_settings().sales_dashboard_password or "").strip()


def enabled() -> bool:
    return bool(configured_password())


def _key(password: str) -> bytes:
    return hashlib.sha256(("sales-audit-gate|" + password).encode()).digest()


def make_token(password: str, now: float | None = None) -> str:
    exp = int((now if now is not None else time.time()) + SESSION_SECONDS)
    sig = hmac.new(_key(password), str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def valid_token(token: str | None, password: str, now: float | None = None) -> bool:
    if not token or not password or "." not in token:
        return False
    exp, _, sig = token.partition(".")
    if not exp.isdigit() or int(exp) < (now if now is not None else time.time()):
        return False
    good = hmac.new(_key(password), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good)


def password_ok(supplied: str | None, password: str) -> bool:
    """Constant-time comparison (hash both sides first so length doesn't leak)."""
    if not supplied or not password:
        return False
    return hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(), hashlib.sha256(password.encode()).digest())


_SAFE_NEXT = re.compile(r"^/reports/sales-audit(?:[/?#][A-Za-z0-9_\-./?=&%#:+,]*)?$")


def safe_next(value: str | None) -> str:
    """Only ever redirect back into the dashboard -- never to another path or host (no open redirect)."""
    v = (value or "").strip()
    return v if v and "//" not in v and _SAFE_NEXT.match(v) else BASE


class _Throttle:
    """In-memory, per process: enough to stop password guessing against one instance."""

    def __init__(self) -> None:
        self._fails: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def retry_after(self, ip: str, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        with self._lock:
            q = self._fails[ip]
            while q and q[0] < now - FAILURE_WINDOW:
                q.popleft()
            return int(q[0] + FAILURE_WINDOW - now) + 1 if len(q) >= MAX_FAILURES else 0

    def record_failure(self, ip: str, now: float | None = None) -> None:
        with self._lock:
            self._fails[ip].append(now if now is not None else time.time())

    def clear(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)

    def reset_all(self) -> None:
        with self._lock:
            self._fails.clear()


throttle = _Throttle()


def client_ip(headers: dict[str, str], peer: str | None) -> str:
    """Railway/Cloudflare put the real client first in X-Forwarded-For."""
    fwd = headers.get("cf-connecting-ip") or headers.get("x-forwarded-for") or ""
    return (fwd.split(",")[0].strip() or peer or "unknown")


def _headers(scope: Scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def request_is_authorized(headers: dict[str, str], password: str) -> bool:
    if password_ok(headers.get("x-sales-password"), password):
        return True
    jar = SimpleCookie()
    try:
        jar.load(headers.get("cookie", ""))
    except Exception:
        return False
    morsel = jar.get(COOKIE)
    return valid_token(morsel.value if morsel else None, password)


def _is_page(method: str, path: str) -> bool:
    return method == "GET" and (path == BASE or path.startswith(f"{BASE}/view/"))


def _is_open(path: str) -> bool:
    return path in (LOGIN_PATH, LOGOUT_PATH) or path.startswith(f"{BASE}/assets/")


class SalesAccessMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        password = configured_password() if scope["type"] == "http" else ""
        path = scope.get("path", "")
        if not password or not (path == BASE or path.startswith(BASE + "/")) or _is_open(path):
            await self.app(scope, receive, send)
            return
        if request_is_authorized(_headers(scope), password):
            await self.app(scope, receive, send)
            return
        if _is_page(scope.get("method", "GET"), path):
            qs = scope.get("query_string", b"").decode("latin-1")
            target = path + (f"?{qs}" if qs else "")
            await RedirectResponse(f"{LOGIN_PATH}?next={quote(target, safe='')}", status_code=303)(scope, receive, send)
            return
        await JSONResponse({"detail": "access password required"}, status_code=401)(scope, receive, send)
