"""Liveness and readiness endpoints (development plan §7.2 Operations).

Liveness answers as long as the process runs. Readiness reports whether the instance can actually
serve: with a database configured it runs a bounded `SELECT 1`, so a database outage fails
readiness rather than reporting a healthy instance that cannot write (V-P7-06). Readiness also
fails while a restore is waiting for tombstone reconciliation, so a half-restored instance never
opens (V-P7-20).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from server.config import PRODUCT_NAME
from server.ops import restore_gate

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "product": PRODUCT_NAME}


def database_ready(request: Request) -> tuple[bool, str]:
    """`SELECT 1` on the configured database; the engine's connect timeout bounds the wait."""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        return False, "database not configured"
    try:
        with factory() as session:
            session.execute(text("SELECT 1"))
    except Exception as exc:
        log.warning("readiness database probe failed: %s", type(exc).__name__)
        return False, type(exc).__name__
    return True, "ok"


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, Any]:
    settings = request.app.state.settings
    configured = settings.database_url is not None
    ready, detail = database_ready(request) if configured else (False, "database not configured")
    restore = restore_gate.pending(restore_gate.marker_path(settings.bootstrap_state_path))
    if restore is not None:
        ready = False  # a restore is loaded but its key tombstones are not reconciled yet
    if not ready:
        response.status_code = 503
    body: dict[str, Any] = {
        "status": "ok" if ready else "unavailable",
        "product": PRODUCT_NAME,
        "database_configured": configured,
        "database": detail,
    }
    if restore is not None:
        body["restore"] = {
            "pending": True,
            "backup_id": restore.get("backup_id", ""),
            "reason": restore.get("reason", ""),
        }
    return body
