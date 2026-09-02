"""Liveness/readiness endpoints (development plan §7.2 Operations)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from server.config import PRODUCT_NAME

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "product": PRODUCT_NAME}


@router.get("/readyz")
async def readyz(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    # Phase 0: readiness reports configuration presence only; DB probes arrive with P1-01.
    return {
        "status": "ok",
        "product": PRODUCT_NAME,
        "database_configured": settings.database_url is not None,
    }
