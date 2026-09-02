"""Deferred integrity job (development plan §6.3): recompute every aggregate chain and check
workspace consistency of actor/channel/task/caused_by. Returns problems; never modifies rows."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.events.hashing import verify_chain
from server.events.postgres_store import _COLUMNS, row_to_event


def verify_events(session: Session, workspace_id: str | None = None) -> list[str]:
    where = "WHERE workspace_id = :ws" if workspace_id else ""
    params: dict[str, Any] = {"ws": workspace_id} if workspace_id else {}
    order = "ORDER BY workspace_id, aggregate_type, aggregate_id, aggregate_seq"
    rows = session.execute(
        text(f"SELECT {_COLUMNS} FROM events {where} {order}"),  # noqa: S608 - constant SQL parts
        params,
    ).mappings()
    problems: list[str] = []
    streams: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        ev = row_to_event(row)
        streams.setdefault(
            (ev["workspace_id"], ev["aggregate_type"], ev["aggregate_id"]), []
        ).append(ev)
    for (ws, agg_type, agg_id), events in streams.items():
        for p in verify_chain(events):
            problems.append(f"{agg_type}/{agg_id}: {p}")
        for ev in events:
            if ev["caused_by"]:
                cause_ws = session.execute(
                    text("SELECT workspace_id FROM events WHERE event_id = :e"),
                    {"e": ev["caused_by"]},
                ).scalar()
                if cause_ws is None or str(cause_ws) != ws:
                    problems.append(f"{ev['event_id']}: caused_by outside workspace or missing")
            actor_ws = session.execute(
                text("SELECT workspace_id FROM accounts WHERE id = :a"),
                {"a": ev["actor_account_id"]},
            ).scalar()
            if actor_ws is None or str(actor_ws) != ws:
                problems.append(f"{ev['event_id']}: actor outside workspace")
    return problems
