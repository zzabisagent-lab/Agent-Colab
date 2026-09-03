"""Audit explorer (P4-02; V-P4-23): search by period/actor/action/target with stable cursor
pagination, and CSV/JSONL export. Rows are served exactly as stored: ``redacted_metadata`` is
never de-redacted (the audit chain holds only redacted metadata by construction)."""

from __future__ import annotations

import base64
import csv
import datetime as dt
import io
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

MAX_LIMIT = 100
COLUMNS = (
    "audit_id",
    "occurred_at",
    "actor_label",
    "actor_account_id",
    "action",
    "target_type",
    "target_id",
    "result",
    "error_code",
    "correlation_id",
    "redacted_metadata",
    "content_hash",
)


@dataclass(frozen=True)
class AuditQuery:
    workspace_id: uuid.UUID
    since: dt.datetime | None = None
    until: dt.datetime | None = None
    actor: str | None = None  # actor_label or public account id
    action: str | None = None  # exact, or prefix with a trailing '*'
    target_type: str | None = None
    target_id: str | None = None
    result: str | None = None
    limit: int = 50
    cursor: str | None = None


def encode_cursor(occurred_at: dt.datetime, row_id: int) -> str:
    raw = json.dumps({"t": occurred_at.isoformat(), "id": row_id}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[dt.datetime, int]:
    padded = cursor + "=" * (-len(cursor) % 4)
    data = json.loads(base64.urlsafe_b64decode(padded.encode()))
    return dt.datetime.fromisoformat(data["t"]), int(data["id"])


def _where(q: AuditQuery) -> tuple[str, dict[str, Any]]:
    clauses = ["workspace_id = :w"]
    params: dict[str, Any] = {"w": q.workspace_id}
    if q.since is not None:
        clauses.append("occurred_at >= :since")
        params["since"] = q.since
    if q.until is not None:
        clauses.append("occurred_at < :until")
        params["until"] = q.until
    if q.actor:
        clauses.append("actor_label = :actor")
        params["actor"] = q.actor
    if q.action:
        if q.action.endswith("*"):
            clauses.append("action LIKE :action")
            params["action"] = q.action[:-1] + "%"
        else:
            clauses.append("action = :action")
            params["action"] = q.action
    if q.target_type:
        clauses.append("target_type = :tt")
        params["tt"] = q.target_type
    if q.target_id:
        clauses.append("target_id = :ti")
        params["ti"] = q.target_id
    if q.result:
        clauses.append("result = :res")
        params["res"] = q.result
    if q.cursor:
        after_t, after_id = decode_cursor(q.cursor)
        clauses.append("(occurred_at, id) > (:ct, :cid)")
        params["ct"] = after_t
        params["cid"] = after_id
    return " AND ".join(clauses), params


def _row_dict(r: Any) -> dict[str, Any]:
    return {
        "audit_id": str(r["audit_id"]),
        "occurred_at": r["occurred_at"].isoformat(),
        "actor_label": str(r["actor_label"]),
        "actor_account_id": None if r["actor_account_id"] is None else str(r["actor_account_id"]),
        "action": str(r["action"]),
        "target_type": str(r["target_type"]),
        "target_id": str(r["target_id"]),
        "result": str(r["result"]),
        "error_code": r["error_code"],
        "correlation_id": str(r["correlation_id"]),
        "redacted_metadata": r["redacted_metadata"],
        "content_hash": str(r["content_hash"]),
    }


def search(session: Session, q: AuditQuery) -> dict[str, Any]:
    limit = max(1, min(int(q.limit), MAX_LIMIT))
    where, params = _where(q)
    sql = (
        "SELECT id, audit_id, occurred_at, actor_label, actor_account_id, action, "  # noqa: S608
        "target_type, target_id, result, error_code, correlation_id, redacted_metadata, "
        "content_hash FROM audit_events WHERE " + where + " ORDER BY occurred_at, id LIMIT :lim"
    )
    rows = session.execute(text(sql), {**params, "lim": limit + 1}).mappings().all()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit and page:
        last = page[-1]
        next_cursor = encode_cursor(last["occurred_at"], int(last["id"]))
    return {"items": [_row_dict(r) for r in page], "next_cursor": next_cursor, "limit": limit}


def iter_export(session: Session, q: AuditQuery, *, batch: int = 500) -> Iterator[dict[str, Any]]:
    cursor = q.cursor
    while True:
        page = search(
            session, AuditQuery(**{**q.__dict__, "limit": min(batch, MAX_LIMIT), "cursor": cursor})
        )
        yield from page["items"]
        cursor = page["next_cursor"]
        if cursor is None:
            return


def export_jsonl(session: Session, q: AuditQuery) -> Iterator[str]:
    for item in iter_export(session, q):
        yield json.dumps(item, sort_keys=True) + "\n"


def export_csv(session: Session, q: AuditQuery) -> Iterator[str]:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(COLUMNS))
    writer.writeheader()
    yield buf.getvalue()
    for item in iter_export(session, q):
        buf.seek(0)
        buf.truncate()
        row = dict(item)
        row["redacted_metadata"] = json.dumps(item["redacted_metadata"], sort_keys=True)
        writer.writerow(row)
        yield buf.getvalue()
