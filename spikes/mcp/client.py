"""Spike client for V-P0-17: long-poll timing, redelivery after reconnect, subscribe.

Run with the spike server up: uv run python -m spikes.mcp.client > evidence.jsonl
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

import anyio

from mcp.client import subscriptions as cs
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = f"http://127.0.0.1:{os.environ.get('SPIKE_PORT', '8765')}/mcp"
AGENT = "agent-spike-0001"
ITEM = "wi-00000000000000ff"


def log(event: str, **fields: Any) -> None:
    print(json.dumps({"t": time.time(), "event": event, **fields}), flush=True)


def _payload(result: Any) -> Any:
    sc = getattr(result, "structuredContent", None)
    if sc:
        return sc.get("result", sc)
    text = result.content[0].text if result.content else "{}"
    return json.loads(text)


async def _call(session: ClientSession, name: str, **args: Any) -> Any:
    return _payload(await session.call_tool(name, args))


async def scenario_a_empty_long_poll() -> float:
    async with (
        streamable_http_client(URL) as (r, w, *_),
        ClientSession(r, w, read_timeout_seconds=60) as s,
    ):
        await s.initialize()
        t0 = time.time()
        out = await _call(s, "work_poll", agent_id=AGENT, max_wait_s=30)
        elapsed = time.time() - t0
        log("a_empty_poll", elapsed_s=round(elapsed, 3), items=out["items"])
        assert out["items"] == [] and elapsed <= 30.5, elapsed
        return elapsed


async def scenario_b_redelivery_after_reconnect() -> tuple[int, int]:
    async with streamable_http_client(URL) as (r, w, *_), ClientSession(r, w) as s:
        await s.initialize()
        await _call(s, "enqueue", agent_id=AGENT, work_item_id=ITEM)
        t0 = time.time()
        out = await _call(s, "work_poll", agent_id=AGENT, max_wait_s=30)
        log("b_first_poll", elapsed_s=round(time.time() - t0, 3), items=out["items"])
        first_count = out["items"][0]["delivery_count"]
        log("b_disconnect_without_ack", session_id=s.protocol_version)
    # session closed without ack -> reconnect with a fresh session
    async with streamable_http_client(URL) as (r, w, *_), ClientSession(r, w) as s:
        await s.initialize()
        t0 = time.time()
        out = await _call(s, "work_poll", agent_id=AGENT, max_wait_s=30)
        log("b_reconnect_poll", elapsed_s=round(time.time() - t0, 3), items=out["items"])
        assert [i["work_item_id"] for i in out["items"]] == [ITEM]
        second_count = out["items"][0]["delivery_count"]
        assert second_count == first_count + 1
        return first_count, second_count


async def scenario_c_ack_stops_redelivery() -> None:
    async with streamable_http_client(URL) as (r, w, *_), ClientSession(r, w) as s:
        await s.initialize()
        await _call(s, "work_ack", agent_id=AGENT, work_item_id=ITEM)
        res1 = await _call(s, "work_result", agent_id=AGENT, work_item_id=ITEM, result_ref="res-1")
        res2 = await _call(s, "work_result", agent_id=AGENT, work_item_id=ITEM, result_ref="res-2")
        log("c_results", first=res1, duplicate=res2)
        assert res1["code"] == "RESULT_ACCEPTED" and res2["code"] == "DUPLICATE_RESULT_IGNORED"
        t0 = time.time()
        out = await _call(s, "work_poll", agent_id=AGENT, max_wait_s=2)
        log("c_poll_after_ack", elapsed_s=round(time.time() - t0, 3), items=out["items"])
        assert out["items"] == []


async def scenario_d_subscribe() -> dict[str, Any]:
    uri = f"colab://inbox/{AGENT}"
    received: list[str] = []
    outcome: dict[str, Any] = {"supported": False, "events": received}
    async with streamable_http_client(URL) as (r, w, *_), ClientSession(r, w) as s:
        await s.initialize()
        log("d_protocol_version", version=s.protocol_version)
        try:
            async with cs.listen(s, resource_subscriptions=[uri]) as sub:
                outcome["supported"] = True

                async def producer() -> None:
                    await asyncio.sleep(0.5)
                    async with (
                        streamable_http_client(URL) as (r2, w2, *_),
                        ClientSession(r2, w2) as s2,
                    ):
                        await s2.initialize()
                        await _call(
                            s2, "enqueue", agent_id=AGENT, work_item_id="wi-00000000000000ee"
                        )

                async def consumer() -> None:
                    with anyio.move_on_after(10):
                        async for event in sub:
                            received.append(type(event).__name__ + ":" + getattr(event, "uri", ""))
                            log("d_notification", event=received[-1])
                            break

                async with anyio.create_task_group() as tg:
                    tg.start_soon(producer)
                    tg.start_soon(consumer)
        except cs.ListenNotSupportedError as exc:
            log("d_listen_unsupported", detail=str(exc))
        except Exception as exc:
            log("d_listen_error", error=type(exc).__name__, detail=str(exc)[:300])
    log("d_subscribe_outcome", **outcome)
    return outcome


async def scenario_d2_legacy_subscribe() -> dict[str, Any]:
    """Pre-2026-07-28 path: resources/subscribe + notifications/resources/updated."""
    uri = f"colab://inbox/{AGENT}"
    received: list[str] = []
    got = asyncio.Event()

    async def handler(message: Any) -> None:
        name = type(message).__name__
        if "ResourceUpdated" in name:
            received.append(f"{name}:{getattr(message.params, 'uri', '')}")
            log("d2_notification", event=received[-1])
            got.set()

    outcome: dict[str, Any] = {"supported": False, "events": received}
    async with (
        streamable_http_client(URL) as (r, w, *_),
        ClientSession(r, w, message_handler=handler) as s,
    ):
        await s.initialize()
        try:
            await s.subscribe_resource(uri)
            outcome["subscribe_accepted"] = True
        except Exception as exc:
            log("d2_subscribe_error", error=type(exc).__name__, detail=str(exc)[:300])
            outcome["subscribe_accepted"] = False
            log("d2_outcome", **outcome)
            return outcome
        async with streamable_http_client(URL) as (r2, w2, *_), ClientSession(r2, w2) as s2:
            await s2.initialize()
            await _call(s2, "enqueue", agent_id=AGENT, work_item_id="wi-00000000000000dd")
        try:
            await asyncio.wait_for(got.wait(), timeout=10)
            outcome["supported"] = True
        except TimeoutError:
            log("d2_no_notification_within_10s")
    log("d2_outcome", **outcome)
    return outcome


async def main() -> int:
    log("spike_start", url=URL)
    a = await scenario_a_empty_long_poll()
    b = await scenario_b_redelivery_after_reconnect()
    await scenario_c_ack_stops_redelivery()
    d = await scenario_d_subscribe()
    d2 = await scenario_d2_legacy_subscribe()
    log(
        "spike_summary",
        empty_poll_s=round(a, 3),
        delivery_counts=list(b),
        listen=d,
        legacy_subscribe=d2,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
