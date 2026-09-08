"""Operator entry point for private-network provider intake; credentials come only from env."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import text

from server.channels.telegram.intake import InboundHandler, poll_updates
from server.channels.telegram.provider import client_from_env


class FileOffsetStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, provider_instance_id: str) -> int | None:
        if not self.path.exists():
            return None
        data = json.loads(self.path.read_text())
        value = data.get(provider_instance_id)
        return int(value) if value is not None else None

    def save(self, provider_instance_id: str, offset: int) -> None:
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        data[provider_instance_id] = offset
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(self.path)


def poll_once(client: Any, instance: str, handler: InboundHandler, store: FileOffsetStore) -> None:
    for _ in poll_updates(client, instance, handler, store, max_rounds=1):
        pass


def wire_telegram_test_client(app: Any) -> None:
    app.state.telegram_client = client_from_env()


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent-Colab private-network connection relay")
    parser.add_argument(
        "mode",
        choices=["telegram-register", "telegram-poll", "telegram-webhook", "mattermost-listen"],
    )
    parser.add_argument("--workspace", help="Workspace UUID (required for registration)")
    parser.add_argument("--callback-url", help="Public HTTPS Telegram webhook URL")
    parser.add_argument(
        "--offset-file", type=Path, default=Path("/var/lib/agent-colab/telegram-offset.json")
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        from server.api.dispatch import default_runtime
        from server.channels.gateway import build_gateway
        from server.config import Settings
        from server.db.engine import make_engine, make_session_factory, session_scope

        settings = Settings()
        if not settings.database_url:
            raise ValueError("database configuration required")
        engine = make_engine(settings.database_url)
        runtime = default_runtime(make_session_factory(engine), settings)
        gateway = build_gateway(runtime)
        if args.mode == "mattermost-listen":
            from server.channels.mattermost.websocket import WebSocketSubscriber

            async def receive(event: dict[str, Any]) -> None:
                gateway.on_mattermost_event(event)

            subscriber = WebSocketSubscriber(
                os.environ["AGENT_COLAB_MATTERMOST_URL"],
                os.environ["AGENT_COLAB_MATTERMOST_BOT_TOKEN"],
                receive,
            )
            asyncio.run(subscriber.run_once() if args.once else subscriber.run())
            return 0
        client = client_from_env()
        if client is None:
            raise ValueError("Telegram credential required")
        bot_id = str(client.get_me()["id"])
        instance = "tg:" + bot_id
        if args.mode == "telegram-register":
            workspace = uuid.UUID(args.workspace)
            with session_scope(runtime.session_factory) as session:
                existing = session.execute(
                    text(
                        "SELECT workspace_id FROM provider_instances WHERE "
                        "provider_instance_id = :p"
                    ),
                    {"p": instance},
                ).first()
                if existing is not None and str(existing[0]) != str(workspace):
                    raise ValueError("provider belongs to another workspace")
                session.execute(
                    text(
                        "INSERT INTO provider_instances (id, provider_instance_id, "
                        "workspace_id, provider, base_url, team_or_bot_ref) "
                        "VALUES (:i, :p, :w, 'telegram', 'https://api.telegram.org', :b) "
                        "ON CONFLICT (provider_instance_id) DO NOTHING"
                    ),
                    {"i": uuid.uuid4(), "p": instance, "w": workspace, "b": bot_id},
                )
            print("Agent-Colab Telegram provider registered: " + instance)
        elif args.mode == "telegram-webhook":
            if not args.callback_url or not args.callback_url.startswith("https://"):
                raise ValueError("HTTPS callback required")
            client.set_webhook(args.callback_url, os.environ["AGENT_COLAB_TELEGRAM_WEBHOOK_SECRET"])
            print("Agent-Colab Telegram webhook configured")
        else:
            # Deleting a webhook is an explicit operator action before selecting polling mode.
            with session_scope(runtime.session_factory) as session:
                row = session.execute(
                    text(
                        "SELECT 1 FROM provider_instances WHERE provider_instance_id = :p "
                        "AND provider = 'telegram' AND status = 'active'"
                    ),
                    {"p": instance},
                ).first()
                if row is None:
                    raise ValueError("register active provider first")
            store = FileOffsetStore(args.offset_file)
            while True:
                poll_once(client, instance, gateway.on_telegram_message, store)
                if args.once:
                    break
        engine.dispose()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("Agent-Colab connection relay failed; check provider configuration and permissions.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
