"""P3-10: one concurrent ``work_poll`` per MCP session → ``MCP_POLL_IN_PROGRESS``."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.agents import transport_mcp as tm
from server.identity.principals import Principal


class _Ctx:
    session_id = "sess-1"


class _Server:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}
        self.resources: dict[str, Any] = {}
        self._subscriptions = None

        class _Low:
            def add_request_handler(self, *a: Any, **k: Any) -> None:
                return None

        self._lowlevel_server = _Low()

    def tool(self, name: str) -> Any:
        def deco(fn: Any) -> Any:
            self.tools[name] = fn
            return fn

        return deco

    def resource(self, uri: str, **_: Any) -> Any:
        def deco(fn: Any) -> Any:
            self.resources[uri] = fn
            return fn

        return deco


PRINCIPAL = Principal("acct-a", "00000000-0000-0000-0000-000000000001", "agent", "sha256:x")


@pytest.mark.anyio
async def test_second_poll_in_same_session_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_run(*_: Any, **kw: Any) -> dict[str, Any]:
        calls.append(kw["name"])
        return {"items": [], "delivered": 0}

    monkeypatch.setattr(tm, "_run", fake_run)
    srv = _Server()
    tm.register_work_transport(srv, object(), lambda: PRINCIPAL)  # type: ignore[arg-type]
    poll = srv.tools["work_poll"]
    first = asyncio.create_task(poll(agent_id="agent-a", max_wait_s=1.5, ctx=_Ctx()))
    await asyncio.sleep(0.2)
    second = await poll(agent_id="agent-a", max_wait_s=1.5, ctx=_Ctx())
    assert second["error"]["code"] == "MCP_POLL_IN_PROGRESS" and second["error"]["status"] == 429
    out = await first
    assert out["items"] == [] and out["waited_s"] >= 0.9  # 1.5 s minus the safety margin

    # a different session polls independently
    class _Other:
        session_id = "sess-2"

    assert "error" not in await poll(agent_id="agent-a", max_wait_s=0, ctx=_Other())
    assert tm.LONG_POLL_MAX_S == 30 and tm.SAFETY_MARGIN_S < 1


def test_registered_tools_and_resources() -> None:
    srv = _Server()
    tm.register_work_transport(srv, object(), lambda: PRINCIPAL)  # type: ignore[arg-type]
    assert set(srv.tools) >= {
        "work_poll",
        "work_ack",
        "work_start",
        "work_reject",
        "work_result",
        "task_get",
        "document_get",
    }
    assert set(srv.resources) == {tm.INBOX_URI, tm.TASK_URI, tm.DOCUMENT_URI}
