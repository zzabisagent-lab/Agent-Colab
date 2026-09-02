from __future__ import annotations

import datetime as dt
from typing import Any

from server.channels.gateway import ChannelGateway, providers_from_env
from server.domain.clock import FixedClock


class _Bridge:
    def __init__(self) -> None:
        self.mm: list[Any] = []
        self.tg: list[Any] = []

    def on_mattermost_post(self, session: Any, clock: Any, view: Any) -> list[Any]:
        self.mm.append(view)
        return []

    def on_telegram_message(self, session: Any, clock: Any, msg: Any) -> list[Any]:
        self.tg.append(msg)
        return []

    def deliver(
        self, session: Any, providers: Any, clock: Any, ws: str, **_: Any
    ) -> dict[str, int]:
        return {"sent": 0}


class _Session:
    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, *a: Any, **k: Any) -> Any:
        class R:
            def first(self) -> tuple[str] | None:
                return ("mm:test",)

        return R()

    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


class _Runtime:
    session_factory = staticmethod(lambda: _Session())
    clock = FixedClock(dt.datetime(2026, 8, 1, tzinfo=dt.UTC))

    def resolve_workspace(self, session: Any, account_uuid: str | None = None) -> str:
        return "ws"


def test_websocket_posted_events_reach_the_bridge_and_edits_do_not() -> None:
    bridge = _Bridge()
    gw = ChannelGateway(runtime=_Runtime(), clock=_Runtime.clock, bridge=bridge)  # type: ignore[arg-type]
    gw.on_mattermost_event({"event": "post_edited", "post": {"id": "p1", "channel_id": "c"}})
    gw.on_mattermost_event(
        {
            "event": "posted",
            "post": {"id": "p1", "channel_id": "c", "user_id": "u", "message": "hi", "props": {}},
        }
    )
    assert (
        len(bridge.mm) == 1
        and bridge.mm[0].post_id == "p1"
        and bridge.mm[0].provider_instance_id == "mm:test"
    )


def test_telegram_inbound_hooks_run_before_the_relay() -> None:
    bridge = _Bridge()
    handled: list[str] = []

    def hook(session: Any, msg: Any) -> bool:
        handled.append("hook")
        return msg == "command"

    gw = ChannelGateway(
        runtime=_Runtime(), clock=_Runtime.clock, bridge=bridge, inbound_hooks=[hook]
    )  # type: ignore[arg-type]
    gw.on_telegram_message("command")  # type: ignore[arg-type]
    gw.on_telegram_message("chat")  # type: ignore[arg-type]
    assert handled == ["hook", "hook"] and bridge.tg == ["chat"]


def test_providers_absent_without_credentials(monkeypatch: Any) -> None:
    for key in (
        "AGENT_COLAB_MATTERMOST_URL",
        "AGENT_COLAB_MATTERMOST_BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    assert providers_from_env() == {}
    gw = ChannelGateway(runtime=_Runtime(), clock=_Runtime.clock, bridge=_Bridge())  # type: ignore[arg-type]
    assert gw.drain_once() == {}
