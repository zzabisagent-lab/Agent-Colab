"""Problem Details (RFC 9457) responses with stable error codes (development plan §7.1)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

PROBLEM_JSON = "application/problem+json"
RETRY_AFTER_S = 30
log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str, extra: dict[str, Any] | None = None):
        super().__init__(f"{code}: {detail}")
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra or {}


def problem(status: int, code: str, detail: str, **extra: Any) -> JSONResponse:
    body = {
        "type": f"https://agent-colab.dev/errors/{code}",
        "title": code,
        "status": status,
        "detail": detail,
        "code": code,
        **extra,
    }
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_JSON)


async def api_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return problem(exc.status, exc.code, exc.detail, **exc.extra)


async def database_unavailable_handler(_: Request, exc: Exception) -> JSONResponse:
    """A database outage is an availability failure, not a server defect (V-P7-06).

    Connection and operational errors from the driver answer 503 with a stable code and a
    ``Retry-After`` hint, so a caller can distinguish "try again" from a real 500. The detail
    never carries the connection string.
    """
    response = problem(
        503,
        "DATABASE_UNAVAILABLE",
        "the database is unavailable; the request was not applied",
        retry_after_s=RETRY_AFTER_S,
    )
    response.headers["Retry-After"] = str(RETRY_AFTER_S)
    log.warning("database unavailable: %s", type(exc).__name__)
    return response
