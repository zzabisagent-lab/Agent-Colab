"""Shared harness for the Setup Wizard / Settings / Maintenance tests (P4-03/04/13)."""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from server.api.setup import build_service
from server.config import Settings
from server.db.engine import normalize_url
from server.domain.clock import FixedClock
from server.main import create_app
from server.security import reauth
from server.setup.wizard import SetupService

T0 = dt.datetime(2026, 1, 5, 9, 0, tzinfo=dt.UTC)  # past: system-clock authorizers accept it
TEST_URL = os.environ.get("AGENT_COLAB_TEST_DATABASE_URL", "")


class LoopbackShim:
    """Presents every request as coming from 127.0.0.1 (TestClient reports 'testclient')."""

    def __init__(self, app: Any, client: tuple[str, int] = ("127.0.0.1", 5555)) -> None:
        self.app = app
        self.client = client

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            scope["client"] = self.client
        await self.app(scope, receive, send)


def fake_mattermost_probe(url: str, token: str) -> dict[str, Any]:
    if not token.startswith("mm-bot-"):
        raise RuntimeError("401 Unauthorized")
    return {"id": "bot-user-1", "username": "agent-colab", "is_bot": True}


def fresh_database() -> Iterator[str]:
    """An EMPTY database (no migrations) created from the maintenance URL; dropped afterwards."""
    base = normalize_url(TEST_URL)
    maint = create_engine(base, isolation_level="AUTOCOMMIT")
    name = f"colab_setup_{uuid.uuid4().hex[:10]}"
    with maint.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.rsplit("/", 1)[0] + f"/{name}"
    try:
        yield url
    finally:
        with maint.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        maint.dispose()


def db_parts(url: str) -> dict[str, Any]:
    parsed = urlparse(url.replace("postgresql+psycopg://", "postgresql://"))
    return {
        "db_host": parsed.hostname or "127.0.0.1",
        "db_port": parsed.port or 5432,
        "db_name": parsed.path.lstrip("/"),
        "db_user": parsed.username or "",
        "db_password": parsed.password or "",
    }


class Wizard:
    """A wizard app + client bound to a temp bootstrap store, key path and storage roots."""

    def __init__(
        self, tmp: Path, clock: FixedClock | None = None, database_url: str | None = None
    ) -> None:
        self.tmp = tmp
        self.clock = clock or FixedClock(T0)
        self.store_path = tmp / "bootstrap" / "state.json"
        self.key_path = tmp / "keys" / "master.key"
        self.settings = Settings(
            database_url=database_url,
            bootstrap_state_path=str(self.store_path),
            master_key_b64=None,
        )
        self.app = create_app(self.settings)
        self.app.state.clock = self.clock
        if getattr(self.app.state, "runtime", None) is not None:
            self.app.state.runtime.clock = self.clock
        self.service: SetupService = build_service(self.app)
        self.service.mattermost_probe = fake_mattermost_probe
        self.client = TestClient(LoopbackShim(self.app))
        self.client.__enter__()

    def close(self) -> None:
        self.client.__exit__(None, None, None)

    # ---- wizard steps -------------------------------------------------------------------
    def token(self) -> str:
        r = self.client.post("/setup/token")
        assert r.status_code == 201, r.text
        return str(r.json()["token"])

    def configure_all(
        self,
        db_url: str,
        *,
        bot_token: str = "mm-bot-secret-0001",  # noqa: S107 - fake test token
    ) -> None:
        for section, values in (
            ("db", db_parts(db_url)),
            ("keys", {"secrets.provider": "local", "secrets.master_key_path": str(self.key_path)}),
            ("owner", {"account_id": "acct-owner", "display_name": "System Owner"}),
            (
                "integrations",
                {
                    "instance.name": "Colab Test",
                    "instance.base_url": "http://127.0.0.1:8080",
                    "mattermost.url": "http://mattermost.test:8065",
                    "mattermost.team": "colab",
                    "mattermost.bot_token": bot_token,
                    "storage.artifact_root": str(self.tmp / "artifacts"),
                    "storage.document_root": str(self.tmp / "documents"),
                    "ops.channel_id": "ops-channel",
                },
            ),
        ):
            r = self.client.post("/setup/configure", json={"section": section, "values": values})
            assert r.status_code == 200, r.text

    def preflight(self) -> dict[str, Any]:
        r = self.client.post("/setup/preflight")
        assert r.status_code == 200, r.text
        return dict(r.json())

    def bootstrap(self, token: str) -> Any:
        return self.client.post("/setup/bootstrap", json={"token": token})

    def state(self) -> dict[str, Any]:
        r = self.client.get("/setup/state")
        assert r.status_code == 200, r.text
        return dict(r.json())

    def run_to_locked(self, db_url: str) -> dict[str, Any]:
        token = self.token()
        self.configure_all(db_url)
        assert self.preflight()["ok"]
        r = self.bootstrap(token)
        assert r.status_code == 200, r.text
        body = dict(r.json())
        assert body["state"] == "LOCKED"
        return body


def install_fake_reauth(clock: FixedClock, accounts: set[str] | None = None) -> Callable[[], None]:
    """Every listed account (or all) has a fresh MFA proof; returns a restore function."""

    def verifier(account_uuid: str, session_id: str | None) -> reauth.ReauthProof | None:
        if accounts is not None and account_uuid not in accounts:
            return None
        return reauth.ReauthProof(account_uuid, clock.now(), "totp", session_id)

    reauth.set_verifier(verifier)
    return lambda: reauth.set_verifier(None)
