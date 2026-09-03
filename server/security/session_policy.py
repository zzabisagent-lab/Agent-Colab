"""Cookie-session policy middleware (P4-08/P4-09/P4-10).

For requests authenticated by the session cookie: idle expiry (``security.session_idle_s``,
tracked in ``account_sessions.last_seen_at``), the MFA gate (an Account that must use MFA gets a
session limited to safe methods and the MFA endpoints until it verifies: ``MFA_REQUIRED`` 403),
and break-glass action recording (``X-Break-Glass-Session`` header → audited action row).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from server.db.engine import session_scope
from server.domain.clock import Clock, SystemClock
from server.identity.principals import SESSION_COOKIE, token_hash
from server.security import mfa
from server.security import policy as secpolicy

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MFA_ALLOWED_PREFIXES = ("/api/v1/auth/mfa/", "/api/v1/auth/sessions", "/api/v1/auth/csrf")
BREAK_GLASS_HEADER = "X-Break-Glass-Session"


def _problem(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        {
            "type": f"https://agent-colab.dev/errors/{code}",
            "title": code,
            "status": status,
            "detail": detail,
            "code": code,
        },
        status_code=status,
    )


class SessionPolicyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, *, session_factory: Any, clock: Clock | None = None) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.session_factory = session_factory
        self._clock = clock

    def _now(self, request: Request) -> dt.datetime:
        """The app's clock is resolved per request (tests install a virtual clock later)."""
        state = request.app.state
        clock = getattr(state, "clock", None) or getattr(
            getattr(state, "runtime", None), "clock", None
        )
        return (clock or self._clock or SystemClock()).now()

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        cookie = request.cookies.get(SESSION_COOKIE)
        bg_session = request.headers.get(BREAK_GLASS_HEADER)
        if (not cookie or request.headers.get("Authorization")) and not bg_session:
            return await call_next(request)
        now = self._now(request)
        if cookie and not request.headers.get("Authorization"):
            verdict = self._check_cookie_session(cookie, request, now)
            if verdict is not None:
                return verdict
        response = await call_next(request)
        if bg_session:
            self._record_breakglass_action(bg_session, request, response.status_code, now)
        return response

    def _check_cookie_session(
        self, cookie: str, request: Request, now: dt.datetime
    ) -> Response | None:
        idle_s = secpolicy.int_value("security.session_idle_s")
        with session_scope(self.session_factory) as session:
            row = session.execute(
                text(
                    "SELECT s.id, s.account_id, s.last_seen_at, s.created_at, s.mfa_verified_at, "
                    "a.account_type FROM account_sessions s JOIN accounts a ON a.id = s.account_id "
                    "WHERE s.session_token_hash = :h AND s.revoked_at IS NULL "
                    "AND s.expires_at > :now"
                ),
                {"h": token_hash(cookie), "now": now},
            ).first()
            if row is None:
                return None  # the endpoint's principal resolution answers 401
            last_seen = row[2] or row[3]
            if last_seen is not None and (now - last_seen).total_seconds() > idle_s:
                session.execute(
                    text("UPDATE account_sessions SET revoked_at = :now WHERE id = :i"),
                    {"now": now, "i": row[0]},
                )
                return _problem(401, "SESSION_IDLE_EXPIRED", "session expired after inactivity")
            session.execute(
                text("UPDATE account_sessions SET last_seen_at = :now WHERE id = :i"),
                {"now": now, "i": row[0]},
            )
            if request.method in SAFE_METHODS or request.url.path.startswith(MFA_ALLOWED_PREFIXES):
                return None
            if row[4] is not None:
                return None
            principal = mfa.principal_info(session, str(row[1]))
            if mfa.mfa_required(session, principal, now):
                return _problem(
                    403, "MFA_REQUIRED", "verify MFA (POST /api/v1/auth/mfa/verify) first"
                )
        return None

    def _record_breakglass_action(
        self, bg_session: str, request: Request, status_code: int, now: dt.datetime
    ) -> None:
        from server.security.breakglass import record_action

        with session_scope(self.session_factory) as session:
            record_action(
                session,
                bg_session,
                method=request.method,
                path=request.url.path,
                status_code=status_code,
                correlation_id=request.headers.get("X-Correlation-ID")
                or f"bg-{uuid.uuid4().hex[:12]}",
                now=now,
            )
