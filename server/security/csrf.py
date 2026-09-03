"""Double-submit CSRF protection for cookie sessions (development plan §11.2; P4-08).

Stateless: ``GET /api/v1/auth/csrf`` sets a random token in the ``agent_colab_csrf`` cookie
(SameSite=Strict, not HttpOnly so the console can read it) and returns it; every state-changing
request that authenticates with the session cookie must repeat it in ``X-CSRF-Token``. Requests
that authenticate with ``Authorization: Bearer`` (API clients) are exempt; requests without any
credential are handled by the endpoints themselves (401).
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from server.identity.principals import SESSION_COOKIE

CSRF_COOKIE = "agent_colab_csrf"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
EXEMPT_PREFIXES = ("/mcp", "/setup", "/api/v1/providers/", "/api/v1/agents/")  # signed callbacks


def new_token() -> str:
    return secrets.token_urlsafe(32)


class CsrfMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method in SAFE_METHODS or request.headers.get("Authorization"):
            return await call_next(request)
        if not request.cookies.get(SESSION_COOKIE):
            return await call_next(request)
        path = request.url.path
        if path == "/api/v1/auth/sessions" and request.method == "DELETE":
            return await call_next(request)  # logout is idempotent and harmless
        if any(path.startswith(p) for p in EXEMPT_PREFIXES) and "/webhook/" in path:
            return await call_next(request)
        cookie = request.cookies.get(CSRF_COOKIE, "")
        header = request.headers.get(CSRF_HEADER, "")
        if not cookie or not header or not hmac.compare_digest(cookie, header):
            return JSONResponse(
                {
                    "type": "https://agent-colab.dev/errors/CSRF_TOKEN_INVALID",
                    "title": "CSRF_TOKEN_INVALID",
                    "status": 403,
                    "detail": "state-changing cookie requests need a matching X-CSRF-Token",
                    "code": "CSRF_TOKEN_INVALID",
                },
                status_code=403,
            )
        return await call_next(request)
