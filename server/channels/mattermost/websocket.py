"""Mattermost WebSocket subscriber (development plan §7A.1: ``posted``, ``post_edited``,
``reaction_added``). Authenticates with the bot token, reconnects with bounded backoff, and hands
each event to a handler callback. Inbound events are context only (product principle 4); state
changes still require ``/colab`` commands or interactive actions.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

EVENTS = ("posted", "post_edited", "reaction_added")
Handler = Callable[[dict[str, Any]], Awaitable[None]]


def ws_url(base_url: str) -> str:
    scheme = "wss" if base_url.startswith("https") else "ws"
    host = base_url.split("://", 1)[1].rstrip("/")
    return f"{scheme}://{host}/api/v4/websocket"


def normalize(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Reduce a Mattermost WS frame to ``{event, post, channel_id, user_id, seq}`` or None."""
    event = raw.get("event")
    if event not in EVENTS:
        return None
    data = raw.get("data", {})
    post: dict[str, Any] | None = None
    if isinstance(data.get("post"), str):
        try:
            post = json.loads(data["post"])
        except json.JSONDecodeError:
            post = None
    broadcast = raw.get("broadcast", {})
    return {
        "event": event,
        "post": post,
        "channel_id": broadcast.get("channel_id") or (post or {}).get("channel_id"),
        "user_id": (post or {}).get("user_id") or data.get("user_id"),
        "seq": raw.get("seq"),
        "data": {k: v for k, v in data.items() if k != "post"},
    }


class WebSocketSubscriber:
    def __init__(
        self,
        base_url: str,
        token: str,
        handler: Handler,
        *,
        max_backoff_s: float = 30.0,
    ) -> None:
        self._url = ws_url(base_url)
        self._token = token
        self._handler = handler
        self._max_backoff = max_backoff_s
        self._stop = asyncio.Event()
        self.reconnects = 0

    def stop(self) -> None:
        self._stop.set()

    async def run_once(self) -> None:
        async with websockets.connect(self._url, max_size=4 * 1024 * 1024) as ws:
            await ws.send(
                json.dumps(
                    {"seq": 1, "action": "authentication_challenge", "data": {"token": self._token}}
                )
            )
            while not self._stop.is_set():
                frame = await ws.recv()
                if isinstance(frame, bytes):
                    continue
                try:
                    raw = json.loads(frame)
                except json.JSONDecodeError:
                    continue
                evt = normalize(raw)
                if evt is not None:
                    await self._handler(evt)

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self.run_once()
                backoff = 1.0
            except (OSError, websockets.WebSocketException):
                self.reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
