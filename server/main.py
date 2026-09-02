"""Agent-Colab server entry point (Phase 0 skeleton)."""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from server.api.dispatch import default_runtime
from server.api.errors import ApiError, api_error_handler
from server.api.v1.approvals import router as approvals_router
from server.api.v1.events import router as events_router
from server.api.v1.identity import router as identity_router
from server.api.v1.tasks import router as tasks_router
from server.api.v1.verification import router as verification_router
from server.config import PRODUCT_NAME, Settings, get_settings
from server.db.engine import make_engine, make_session_factory
from server.observability.health import router as health_router

API_VERSION = "v1"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the MCP session manager for the mounted Streamable HTTP app."""
    mcp = getattr(app.state, "mcp", None)
    if mcp is None:
        yield
        return
    async with mcp.session_manager.run():
        yield


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title=PRODUCT_NAME, version="0.0.0", docs_url=None, redoc_url=None, lifespan=_lifespan
    )
    app.state.settings = settings
    app.state.session_factory = (
        make_session_factory(make_engine(settings.database_url)) if settings.database_url else None
    )
    app.state.runtime = (
        default_runtime(app.state.session_factory, settings) if app.state.session_factory else None
    )
    app.add_exception_handler(ApiError, api_error_handler)
    app.include_router(health_router)
    app.include_router(verification_router)
    app.include_router(identity_router)
    app.include_router(events_router)
    app.include_router(tasks_router)
    app.include_router(approvals_router)
    app.state.mcp = None
    if app.state.runtime is not None:
        from server.agents.mcp_server import build_mcp_server

        mcp = build_mcp_server(app.state.runtime, settings.base_url)
        app.state.mcp = mcp
        # mounted last at the root so that the exact path /mcp is served without a redirect
        app.mount("/", mcp.streamable_http_app(streamable_http_path="/mcp"), name="mcp")
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
