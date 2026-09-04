"""Structured JSON logs for requests and commands (P7-02, development plan §20 observability).

One JSON object per line on the application logger, so an operator grepping a log file and an
operator reading the ops dashboard see the same correlation id for the same piece of work
(V-P7-14). The correlation id lives in a :class:`~contextvars.ContextVar` set by the request
middleware, so a command log written deep in a handler inherits the request's id without any
plumbing through call signatures.

Values are never logged: the record carries ids, outcomes and durations only.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from typing import Any

LOGGER_NAME = "agent_colab.access"
COMMAND_LOGGER_NAME = "agent_colab.command"
CORRELATION_HEADER = "X-Correlation-ID"
_UNSET = "-"

#: Fields the JSON formatter copies from ``extra`` onto the record, in a stable order.
FIELDS = (
    "correlation_id",
    "method",
    "path",
    "status",
    "duration_ms",
    "principal_kind",
    "principal",
    "workspace",
    "outcome",
    "command",
    "resource_id",
    "event_id",
    "error_code",
)

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agent_colab_correlation_id", default=_UNSET
)


def new_correlation_id() -> str:
    return "corr-" + uuid.uuid4().hex[:16]


def current_correlation_id() -> str:
    """The correlation id of the work in flight, or ``-`` outside a request."""
    return correlation_id.get()


class JsonFormatter(logging.Formatter):
    """One JSON object per record; unknown extras are dropped rather than guessed at."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, tz=dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, sort_keys=False)


def install_json_logging(level: int = logging.INFO) -> None:
    """Route the access and command loggers through the JSON formatter exactly once."""
    for name in (LOGGER_NAME, COMMAND_LOGGER_NAME):
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.disabled = False
        if not any(getattr(h, "_agent_colab_json", False) for h in logger.handlers):
            handler = logging.StreamHandler()
            handler.setFormatter(JsonFormatter())
            handler._agent_colab_json = True  # type: ignore[attr-defined]
            logger.addHandler(handler)
        logger.propagate = False


def reclaim_root_logging(level: int = logging.INFO) -> list[str]:
    """Take the root logger back from any dependency that reconfigured it, and return what went.

    The MCP server library calls ``logging.basicConfig`` with a ``RichHandler`` when it is
    constructed, which is a reasonable default for a script and wrong for a server: every record
    from SQLAlchemy, uvicorn, alembic and psycopg would then be rendered to a console on stderr as
    a bare message, losing the JSON envelope, the correlation id and every structured field an
    aggregator reads. The application's own loggers do not propagate, so they survive it and the
    breakage is invisible until someone goes looking for a dependency's log line in production.

    So the root logger is restored to one JSON handler after anything that might seize it. Foreign
    handlers are named in the return value rather than dropped silently, because a dependency
    quietly taking over logging is worth a line in the log itself.
    """
    root = logging.getLogger()
    removed = [
        f"{type(h).__module__}.{type(h).__name__}"
        for h in list(root.handlers)
        if not getattr(h, "_agent_colab_json", False)
    ]
    for handler in list(root.handlers):
        if not getattr(handler, "_agent_colab_json", False):
            root.removeHandler(handler)
    if not any(getattr(h, "_agent_colab_json", False) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        handler._agent_colab_json = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(level)
    return removed


def log_command(
    *,
    command: str,
    outcome: str,
    duration_ms: int,
    principal_kind: str = _UNSET,
    principal: str = _UNSET,
    workspace: str = _UNSET,
    resource_id: str | None = None,
    event_id: str | None = None,
    error_code: str | None = None,
) -> None:
    """One line per command execution, carrying the request's correlation id."""
    logging.getLogger(COMMAND_LOGGER_NAME).info(
        "command %s %s",
        command,
        outcome,
        extra={
            "correlation_id": current_correlation_id(),
            "command": command,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "principal_kind": principal_kind,
            "principal": principal,
            "workspace": workspace,
            "resource_id": resource_id,
            "event_id": event_id,
            "error_code": error_code,
        },
    )


Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


class RequestLogMiddleware:
    """Pure-ASGI access log: sets the correlation id, echoes it, and records the outcome.

    Pure ASGI rather than ``BaseHTTPMiddleware`` so a load run does not pay for an extra task
    group per request. Health and metrics reads are logged at DEBUG so a scrape does not drown
    the access log.
    """

    QUIET_PATHS = ("/healthz", "/readyz", "/api/v1/ops/metrics")

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers: Mapping[bytes, bytes] = dict(scope.get("headers") or [])
        incoming = headers.get(CORRELATION_HEADER.lower().encode(), b"").decode() or None
        token = correlation_id.set(incoming or new_correlation_id())
        started = time.perf_counter()
        status_holder = {"status": 500}

        async def _send(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message["status"])
                raw = list(message.get("headers") or [])
                raw.append((CORRELATION_HEADER.encode(), correlation_id.get().encode()))
                message = {**message, "headers": raw}
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            path = str(scope.get("path", ""))
            level = logging.DEBUG if path in self.QUIET_PATHS else logging.INFO
            logging.getLogger(LOGGER_NAME).log(
                level,
                "%s %s %s",
                scope.get("method", "-"),
                path,
                status_holder["status"],
                extra={
                    "correlation_id": correlation_id.get(),
                    "method": str(scope.get("method", "-")),
                    "path": path,
                    "status": status_holder["status"],
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "outcome": "ok" if status_holder["status"] < 400 else "error",
                },
            )
            correlation_id.reset(token)
