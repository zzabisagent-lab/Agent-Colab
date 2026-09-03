"""Maintenance mode (P4-13; V-P4-32).

While active: non-administrative write requests get ``503 + Retry-After``; reads, the outbox
drain and administrative writes continue; the scheduler sees ``scheduler_paused()`` and claims no
due Run. Enter/exit are audited and announced to the ops channel through the notification outbox.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.clock import Clock, SystemClock, isoformat_utc
from server.observability.audit import append_audit

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
EXEMPT_PREFIXES = ("/setup", "/api/v1/auth", "/api/v1/maintenance", "/api/v1/mfa", "/health")
GUARDED_PREFIXES = ("/api/", "/mcp")
_CACHE_TTL_S = 1.0
_cache: dict[str, Any] = {"at": 0.0, "active": False, "retry_after": 300}


def status(session: Session) -> dict[str, Any]:
    row = session.execute(
        text(
            "SELECT active, reason, retry_after_s, entered_by, entered_at, exited_by, exited_at "
            "FROM maintenance_mode WHERE id = 1"
        )
    ).first()
    if row is None:
        return {"active": False, "reason": "", "retry_after_s": 300}
    return {
        "active": bool(row[0]),
        "reason": str(row[1] or ""),
        "retry_after_s": int(row[2]),
        "entered_by": None if row[3] is None else str(row[3]),
        "entered_at": None if row[4] is None else isoformat_utc(row[4]),
        "exited_by": None if row[5] is None else str(row[5]),
        "exited_at": None if row[6] is None else isoformat_utc(row[6]),
    }


def is_active(session: Session) -> bool:
    return bool(status(session)["active"])


def scheduler_paused(session: Session) -> bool:
    """Phase 6 scheduler hook: no due Run is claimed while maintenance mode is active."""
    return is_active(session)


def _announce(
    session: Session,
    *,
    workspace_id: uuid.UUID | None,
    ops_channel: str,
    kind: str,
    reason: str,
    actor_label: str,
    now: dt.datetime,
) -> str | None:
    """One announcement row per enter/exit in the notification outbox (drained by the gateway)."""
    if workspace_id is None or not ops_channel:
        return None
    from server.notifications.outbox import enqueue

    stamp = now.strftime("%Y%m%dT%H%M%S")
    return enqueue(
        session,
        str(workspace_id),
        "notification",
        f"mattermost:{ops_channel}",
        f"maintenance|{kind}|{stamp}",
        {
            "event_type": "MAINTENANCE_MODE_" + kind.upper(),
            "message": f":construction: Maintenance mode {kind} by {actor_label}"
            + (f" — {reason}" if reason else ""),
            "reason": reason,
            "actor": actor_label,
        },
        None,
        now,
    )


def enter(
    session: Session,
    *,
    actor_uuid: uuid.UUID,
    actor_label: str,
    reason: str,
    retry_after_s: int,
    workspace_id: uuid.UUID | None,
    ops_channel: str,
    correlation_id: str,
    clock: Clock | None = None,
) -> dict[str, Any]:
    now = (clock or SystemClock()).now()
    session.execute(
        text(
            "INSERT INTO maintenance_mode (id, active, reason, retry_after_s, entered_by, "
            "entered_at, "
            "exited_by, exited_at) VALUES (1, true, :r, :ra, :by, :at, NULL, NULL) "
            "ON CONFLICT (id) DO UPDATE SET active = true, reason = EXCLUDED.reason, "
            "retry_after_s = EXCLUDED.retry_after_s, entered_by = EXCLUDED.entered_by, "
            "entered_at = EXCLUDED.entered_at, exited_by = NULL, exited_at = NULL"
        ),
        {"r": reason, "ra": retry_after_s, "by": actor_uuid, "at": now},
    )
    audit_id = append_audit(
        session,
        action="maintenance.enter",
        target_type="maintenance",
        target_id="instance",
        result="OK",
        actor_label=actor_label,
        correlation_id=correlation_id,
        workspace_id=workspace_id,
        actor_account_id=actor_uuid,
        metadata={"reason": reason, "retry_after_s": retry_after_s},
        clock=clock,
    )
    announcement = _announce(
        session,
        workspace_id=workspace_id,
        ops_channel=ops_channel,
        kind="entered",
        reason=reason,
        actor_label=actor_label,
        now=now,
    )
    _cache.update({"at": 0.0})
    return {**status(session), "audit_id": audit_id, "announcement_id": announcement}


def exit_mode(
    session: Session,
    *,
    actor_uuid: uuid.UUID,
    actor_label: str,
    workspace_id: uuid.UUID | None,
    ops_channel: str,
    correlation_id: str,
    clock: Clock | None = None,
) -> dict[str, Any]:
    now = (clock or SystemClock()).now()
    current = status(session)
    session.execute(
        text(
            "UPDATE maintenance_mode SET active = false, exited_by = :by, exited_at = :at "
            "WHERE id = 1"
        ),
        {"by": actor_uuid, "at": now},
    )
    audit_id = append_audit(
        session,
        action="maintenance.exit",
        target_type="maintenance",
        target_id="instance",
        result="OK",
        actor_label=actor_label,
        correlation_id=correlation_id,
        workspace_id=workspace_id,
        actor_account_id=actor_uuid,
        metadata={"reason": current.get("reason", ""), "was_active": current.get("active")},
        clock=clock,
    )
    announcement = _announce(
        session,
        workspace_id=workspace_id,
        ops_channel=ops_channel,
        kind="exited",
        reason=str(current.get("reason", "")),
        actor_label=actor_label,
        now=now,
    )
    _cache.update({"at": 0.0})
    return {**status(session), "audit_id": audit_id, "announcement_id": announcement}


def cached_status(session_factory: Any) -> tuple[bool, int]:
    """(active, retry_after_s) with a 1-second process cache (the middleware hot path)."""
    now = time.monotonic()
    if now - float(_cache["at"]) < _CACHE_TTL_S:
        return bool(_cache["active"]), int(_cache["retry_after"])
    try:
        with session_factory() as session:
            current = status(session)
    except Exception:
        return bool(_cache["active"]), int(_cache["retry_after"])
    _cache.update({"at": now, "active": current["active"], "retry_after": current["retry_after_s"]})
    return bool(current["active"]), int(current["retry_after_s"])


def reset_cache() -> None:
    _cache.update({"at": 0.0})


class MaintenanceMiddleware:
    """Pure ASGI: 503 + Retry-After for non-administrative writes while maintenance is active."""

    def __init__(self, app: Any, *, state: Any) -> None:
        self.app = app
        self.state = state  # app.state: session_factory/runtime are resolved per request

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        if (
            method not in WRITE_METHODS
            or not path.startswith(GUARDED_PREFIXES)
            or path.startswith(EXEMPT_PREFIXES)
        ):
            await self.app(scope, receive, send)
            return
        factory = getattr(self.state, "session_factory", None)
        if factory is None:
            await self.app(scope, receive, send)
            return
        active, retry_after = cached_status(factory)
        if not active or self._is_admin(scope, factory):
            await self.app(scope, receive, send)
            return
        body = json.dumps(
            {
                "type": "https://agent-colab.dev/errors/MAINTENANCE_MODE",
                "title": "MAINTENANCE_MODE",
                "status": 503,
                "detail": "the instance is in maintenance mode; retry later",
                "code": "MAINTENANCE_MODE",
                "retry_after_s": retry_after,
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/problem+json"),
                    (b"retry-after", str(retry_after).encode()),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _is_admin(self, scope: Any, factory: Any) -> bool:
        from server.identity.principals import (
            SESSION_COOKIE,
            resolve_service_token,
            resolve_session,
        )

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        cookie_header = headers.get("cookie", "")
        cookie = ""
        for part in cookie_header.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                cookie = value
        runtime = getattr(self.state, "runtime", None)
        if runtime is None or (not auth and not cookie):
            return False
        try:
            with factory() as session:
                principal = None
                if auth.startswith("Bearer "):
                    principal = resolve_service_token(session, auth.removeprefix("Bearer "))
                elif cookie:
                    principal = resolve_session(session, cookie, runtime.clock)
                if principal is None:
                    return False
                runtime.authorizer.require(
                    session,
                    principal.account_id,
                    "ops.manage",
                    action="api:maintenance_enter",
                    correlation_id="maintenance",
                )
                return True
        except Exception:
            return False
