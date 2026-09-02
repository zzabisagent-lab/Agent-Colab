"""Minimal Agent-Colab MCP server spike: work_poll long-poll, work_ack, work_result, inbox resource.

Run: uv run python -m spikes.mcp.server  (127.0.0.1:8765, path /mcp)
In-memory inbox only; items stay in the inbox until acked, so un-acked items are redelivered to
any later poll (including after a reconnect). This is a spike, not product code.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import uvicorn

from mcp.server.mcpserver import MCPServer
from mcp.server.subscriptions import InMemorySubscriptionBus, ResourceUpdated

MAX_WAIT_S = 30
bus = InMemorySubscriptionBus()
server = MCPServer(name="agent-colab-spike", version="0.0.0", subscriptions=bus)
INBOX: dict[str, dict[str, dict[str, Any]]] = {}  # agent_id -> work_item_id -> item
RESULTS: dict[str, str] = {}
WAKEUPS: dict[str, asyncio.Event] = {}
LOG: list[dict[str, Any]] = []


def log(event: str, **fields: Any) -> None:
    LOG.append({"t": time.time(), "event": event, **fields})
    print(json.dumps(LOG[-1]), flush=True)


def _wake(agent_id: str) -> asyncio.Event:
    return WAKEUPS.setdefault(agent_id, asyncio.Event())


@server.tool()
async def enqueue(agent_id: str, work_item_id: str) -> dict[str, Any]:
    """Spike helper: queue a work item for an agent (product code does this internally)."""
    item = {"work_item_id": work_item_id, "agent_id": agent_id, "delivery_count": 0, "acked": False}
    INBOX.setdefault(agent_id, {})[work_item_id] = item
    _wake(agent_id).set()
    await bus.publish(ResourceUpdated(uri=f"colab://inbox/{agent_id}"))
    log("enqueued", agent_id=agent_id, work_item_id=work_item_id)
    return {"queued": work_item_id}


@server.tool()
async def work_poll(agent_id: str, max_wait_s: int = MAX_WAIT_S) -> dict[str, Any]:
    """Long-poll: return un-acked items now, or wait up to max_wait_s (≤ 30) for one."""
    max_wait_s = max(0, min(int(max_wait_s), MAX_WAIT_S))
    started = time.time()
    wake = _wake(agent_id)
    while True:
        pending = [i for i in INBOX.get(agent_id, {}).values() if not i["acked"]]
        if pending:
            for i in pending:
                i["delivery_count"] += 1
            log(
                "poll_delivered",
                agent_id=agent_id,
                items=[i["work_item_id"] for i in pending],
                waited_s=round(time.time() - started, 3),
            )
            return {"items": [dict(i) for i in pending]}
        remaining = max_wait_s - (time.time() - started)
        if remaining <= 0:
            log("poll_empty", agent_id=agent_id, waited_s=round(time.time() - started, 3))
            return {"items": []}
        wake.clear()
        try:
            await asyncio.wait_for(wake.wait(), timeout=remaining)
        except TimeoutError:
            pass


@server.tool()
async def work_ack(agent_id: str, work_item_id: str) -> dict[str, Any]:
    """Acknowledge receipt; the item is no longer redelivered."""
    item = INBOX.get(agent_id, {}).get(work_item_id)
    if item is None:
        return {"error": "WORK_ITEM_NOT_FOUND"}
    item["acked"] = True
    log("acked", agent_id=agent_id, work_item_id=work_item_id)
    return {"acked": work_item_id}


@server.tool()
async def work_result(agent_id: str, work_item_id: str, result_ref: str) -> dict[str, Any]:
    """Idempotent result intake: first result wins, duplicates are ignored."""
    if work_item_id in RESULTS:
        log("duplicate_result_ignored", work_item_id=work_item_id)
        return {"code": "DUPLICATE_RESULT_IGNORED", "first": RESULTS[work_item_id]}
    RESULTS[work_item_id] = result_ref
    log("result_accepted", work_item_id=work_item_id)
    return {"code": "RESULT_ACCEPTED"}


@server.resource("colab://inbox/{agent_id}", mime_type="application/json")
async def inbox(agent_id: str) -> str:
    return json.dumps([i for i in INBOX.get(agent_id, {}).values() if not i["acked"]])


def main() -> None:
    app = server.streamable_http_app(streamable_http_path="/mcp")
    uvicorn.run(
        app, host="127.0.0.1", port=int(os.environ.get("SPIKE_PORT", "8765")), log_level="warning"
    )


if __name__ == "__main__":
    main()
