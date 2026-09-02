"""Problem Details (RFC 9457) responses with stable error codes (development plan §7.1)."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

PROBLEM_JSON = "application/problem+json"


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
