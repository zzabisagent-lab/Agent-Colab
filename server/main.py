"""Agent-Colab server entry point (Phase 0 skeleton)."""

from __future__ import annotations

import argparse

import uvicorn
from fastapi import FastAPI

from server.api.errors import ApiError, api_error_handler
from server.api.v1.verification import router as verification_router
from server.config import PRODUCT_NAME, Settings, get_settings
from server.db.engine import make_engine, make_session_factory
from server.observability.health import router as health_router

API_VERSION = "v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title=PRODUCT_NAME, version="0.0.0", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.session_factory = (
        make_session_factory(make_engine(settings.database_url)) if settings.database_url else None
    )
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(health_router)
    app.include_router(verification_router)
    return app


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-colab", description=f"{PRODUCT_NAME} server")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=args.host or settings.bind_host,
        port=args.port or settings.bind_port,
        log_config=None,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(cli())
