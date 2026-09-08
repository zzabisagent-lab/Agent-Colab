"""Liveness/readiness endpoints (development plan §7.2 Operations)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from server.config import PRODUCT_NAME

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "product": PRODUCT_NAME}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    body: dict[str, object] = {
        "status": "ok",
        "product": PRODUCT_NAME,
        "database_configured": settings.database_url is not None,
    }
    session_factory = getattr(request.app.state, "session_factory", None)
    if settings.database_url is None or session_factory is None:
        body.update({"status": "unready", "database_reachable": False})
        return JSONResponse(body, status_code=503)

    try:
        with session_factory() as session:
            session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        body.update({"status": "unready", "database_reachable": False})
        return JSONResponse(body, status_code=503)

    body["database_reachable"] = True
    return JSONResponse(body)
