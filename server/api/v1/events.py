"""Event query and SSE stream (development plan §7.2 Events, §7.5 SSE envelope, V-P1-11).

``GET /api/v1/events`` — cursor pagination by ``recorded_seq`` (``after``), ``limit`` ≤ 100,
workspace scope, redacted envelope. ``GET /api/v1/events/stream`` — Server-Sent Events with
``id`` = recorded_seq so clients resume with ``Last-Event-ID`` without gaps or duplicates.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from server.api.deps import current_principal
from server.db.engine import session_scope
from server.events.postgres_store import PostgresEventStore
from server.identity.principals import Principal

router = APIRouter(prefix="/api/v1/events", tags=["events"])

ENVELOPE_FIELDS = (
    "event_id",
    "workspace_id",
    "aggregate_type",
    "aggregate_id",
    "aggregate_seq",
    "schema_version",
    "type",
    "occurred_at",
    "recorded_at",
    "correlation_id",
    "caused_by",
    "recorded_seq",
)


def envelope(ev: dict[str, Any]) -> dict[str, Any]:
    """Redacted SSE/list envelope: never the ciphertext, never sensitive content."""
    out = {k: ev.get(k) for k in ENVELOPE_FIELDS}
    out["payload"] = ev.get("payload", {})
    out["has_sensitive"] = ev.get("sensitive_payload_key_ref") is not None
    return out


def _read(request: Request, after: int, limit: int) -> list[dict[str, Any]]:
    runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        store = runtime.store_for(session)
        ws = runtime.resolve_workspace(session)
        assert isinstance(store, PostgresEventStore)
        return [envelope(e) for e in store.read_since(ws, after, limit)]


@router.get("")
def list_events(
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    items = _read(request, after, limit)
    next_cursor = items[-1]["recorded_seq"] if len(items) == limit else None
    return {"items": items, "next_after": next_cursor}


@router.get("/stream")
async def stream_events(
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    after: int = Query(default=0, ge=0),
    max_events: int | None = Query(
        default=None, ge=1, description="test hook: stop after N events"
    ),
    poll_seconds: float = Query(default=1.0, ge=0.05, le=30.0),
) -> StreamingResponse:
    cursor = int(last_event_id) if last_event_id and last_event_id.isdigit() else after

    async def generate() -> AsyncIterator[bytes]:
        nonlocal cursor
        sent = 0
        yield b": connected\n\n"
        while True:
            if await request.is_disconnected():
                return
            batch = await asyncio.to_thread(_read, request, cursor, 100)
            for ev in batch:
                cursor = int(ev["recorded_seq"])
                data = json.dumps(ev, ensure_ascii=False)
                yield f"id: {cursor}\nevent: {ev['type']}\ndata: {data}\n\n".encode()
                sent += 1
                if max_events is not None and sent >= max_events:
                    return
            if not batch:
                if max_events is not None and sent >= max_events:
                    return
                await asyncio.sleep(poll_seconds)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
