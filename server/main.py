"""Agent-Colab server entry point (Phase 0 skeleton)."""

from __future__ import annotations

import argparse
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from server.api.dispatch import default_runtime
from server.api.errors import ApiError, api_error_handler
from server.api.v1.agents import router as agents_router
from server.api.v1.approvals import router as approvals_router
from server.api.v1.auth import router as auth_router
from server.api.v1.bridges import router as bridges_router
from server.api.v1.channel_members import router as channel_members_router
from server.api.v1.channels import router as channels_router
from server.api.v1.events import router as events_router
from server.api.v1.identity import router as identity_router
from server.api.v1.identity_admin import router as identity_admin_router
from server.api.v1.notifications import router as notifications_router
from server.api.v1.providers_mattermost import router as providers_mattermost_router
from server.api.v1.providers_mattermost_actions import router as mattermost_actions_router
from server.api.v1.providers_telegram import router as providers_telegram_router
from server.api.v1.roles import router as roles_router
from server.api.v1.tasks import router as tasks_router
from server.api.v1.verification import router as verification_router
from server.api.v1.work import router as work_router
from server.config import PRODUCT_NAME, Settings, get_settings
from server.db.engine import make_engine, make_session_factory
from server.identity import mattermost_link
from server.observability.health import router as health_router

API_VERSION = "v1"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the MCP session manager for the mounted Streamable HTTP app."""
    mcp = getattr(app.state, "mcp", None)
    gateway = getattr(app.state, "gateway", None)
    if gateway is not None and os.environ.get("AGENT_COLAB_GATEWAY_DRAIN", "1") == "1":
        gateway.start()
    try:
        if mcp is None:
            yield
        else:
            async with mcp.session_manager.run():
                yield
    finally:
        if gateway is not None:
            await gateway.stop()


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
    app.include_router(providers_telegram_router)
    app.include_router(providers_mattermost_router)
    app.include_router(channels_router)
    app.include_router(auth_router)
    app.include_router(bridges_router)
    app.include_router(mattermost_actions_router)
    app.include_router(notifications_router)
    app.include_router(channel_members_router)
    app.include_router(identity_admin_router)
    app.include_router(work_router)  # P3-11: work item REST + signed webhook callbacks
    app.include_router(agents_router)
    app.include_router(roles_router)
    mattermost_link.register()  # P2-13: `link start|confirm` slash handlers
    app.state.telegram_webhook_secret = None  # env AGENT_COLAB_TELEGRAM_WEBHOOK_SECRET by default
    app.state.gateway = None
    app.state.telegram_inbound_handler = None
    app.state.notification_provider = None
    if app.state.runtime is not None:
        from server.channels.gateway import build_gateway
        from server.notifications.providers import (
            CompositeProvider,
            MattermostNotificationProvider,
            NoopProvider,
            SmtpNotificationProvider,
            TelegramRelayGate,
        )

        gateway = build_gateway(app.state.runtime)
        app.state.gateway = gateway
        app.state.telegram_inbound_handler = gateway.on_telegram_message
        app.state.notification_provider = CompositeProvider(
            {
                "mattermost": MattermostNotificationProvider(
                    app.state.session_factory,
                    relay_gate=TelegramRelayGate(),
                    clock=app.state.runtime.clock,
                ),
                "smtp": SmtpNotificationProvider(
                    os.environ.get("AGENT_COLAB_SMTP_HOST"),
                    int(os.environ.get("AGENT_COLAB_SMTP_PORT", "587")),
                    os.environ.get("AGENT_COLAB_SMTP_SENDER", "agent-colab@localhost"),
                ),
                "work_item": NoopProvider(),
            }
        )
    dist = Path(__file__).resolve().parents[1] / "web-admin" / "dist"
    if dist.exists():  # built console (production images copy it here); dev uses Vite's proxy
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        index = dist / "index.html"

        @app.get("/admin/{path:path}", include_in_schema=False)
        async def admin_spa_fallback(path: str) -> FileResponse:
            candidate = dist / path
            if path and candidate.is_file():
                return FileResponse(str(candidate))
            return FileResponse(str(index))

        app.mount("/admin", StaticFiles(directory=str(dist), html=True), name="admin")
    app.state.mcp = None
    if app.state.runtime is not None:
        from server.agents.mcp_server import build_mcp_server

        mcp = build_mcp_server(app.state.runtime, settings.base_url)
        app.state.mcp = mcp
        # mounted last at the root so that the exact path /mcp is served without a redirect
        from server.agents.transport_mcp import MtlsProxyMiddleware

        app.mount(
            "/",
            MtlsProxyMiddleware(mcp.streamable_http_app(streamable_http_path="/mcp")),
            name="mcp",
        )
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
