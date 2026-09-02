"""Projection rebuild and canonical snapshot hashing (V-P1-10, V-P7-08).

``python -m server.projections.runner rebuild tasks`` (needs AGENT_COLAB_DATABASE_URL) or
``snapshot tasks`` prints the snapshot hash. The snapshot is the SHA-256 of the RFC 8785 canonical
JSON of every row of the projection table ordered by its primary key, with every column
included (all values derive deterministically from Events; timestamps come from ``occurred_at``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import os
import sys
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.db.engine import make_engine
from server.events.canonical import canonical_json
from server.events.hashing import sha256_hex
from server.events.postgres_store import _COLUMNS, row_to_event
from server.projections import tasks as _tasks_module  # noqa: F401 - registers the projector
from server.projections.base import Projector, get_projector, projector_names

BATCH = 500
_REPLAY_SQL_TEXT = f"SELECT {_COLUMNS} FROM events WHERE recorded_seq > :after"  # noqa: S608
_REPLAY_SQL = text(_REPLAY_SQL_TEXT + " ORDER BY recorded_seq LIMIT :lim")
_REPLAY_SQL_WS = text(_REPLAY_SQL_TEXT + " AND workspace_id = :ws ORDER BY recorded_seq LIMIT :lim")


def _plain(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.UTC).isoformat(timespec="microseconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).hex()
    return value


def snapshot_hash(session: Session, projection_name: str, workspace_id: str | None = None) -> str:
    """Canonical hash of the projection rows (optionally one Workspace's)."""
    p: Projector = get_projector(projection_name)
    where = "WHERE workspace_id = :ws" if workspace_id else ""
    rows = session.execute(
        text(f"SELECT * FROM {p.table} {where} ORDER BY {p.primary_key}"),  # noqa: S608
        {"ws": workspace_id} if workspace_id else {},
    ).mappings()
    canonical_rows = [{k: _plain(v) for k, v in dict(r).items()} for r in rows]
    return sha256_hex(canonical_json(canonical_rows))


def rebuild(session: Session, projection_name: str, workspace_id: str | None = None) -> str:
    """Delete the projection (optionally one Workspace's rows), replay every Event in recorded
    order, checkpoint; return the snapshot hash."""
    p: Projector = get_projector(projection_name)
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"projection:{p.name}"}
    )
    where = "WHERE workspace_id = :ws" if workspace_id else ""
    session.execute(
        text(f"DELETE FROM {p.table} {where}"),  # noqa: S608 - constant identifier
        {"ws": workspace_id} if workspace_id else {},
    )
    after = 0
    last = 0
    while True:
        params: dict[str, Any] = {"after": after, "lim": BATCH}
        if workspace_id:
            params["ws"] = workspace_id
            rows = session.execute(_REPLAY_SQL_WS, params).mappings().all()
        else:
            rows = session.execute(_REPLAY_SQL, params).mappings().all()
        if not rows:
            break
        for row in rows:
            event = row_to_event(row)
            p.apply(session, event)
            last = int(event["recorded_seq"])
        after = last
    digest = snapshot_hash(session, projection_name, workspace_id)
    session.execute(
        text(
            "INSERT INTO projection_checkpoints "
            "(projection, last_recorded_seq, snapshot_hash, updated_at) "
            "VALUES (:p, :s, :h, now()) ON CONFLICT (projection) DO UPDATE SET last_recorded_seq = "
            "EXCLUDED.last_recorded_seq, snapshot_hash = EXCLUDED.snapshot_hash, updated_at = now()"
        ),
        {"p": p.name, "s": last, "h": digest},
    )
    return digest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="server.projections.runner")
    ap.add_argument("action", choices=["rebuild", "snapshot", "list"])
    ap.add_argument("projection", nargs="?", default="tasks")
    ap.add_argument("--database-url", default=os.environ.get("AGENT_COLAB_DATABASE_URL"))
    ns = ap.parse_args(argv)
    if ns.action == "list":
        print("\n".join(projector_names()))
        return 0
    if not ns.database_url:
        print("AGENT_COLAB_DATABASE_URL required", file=sys.stderr)
        return 2
    engine = make_engine(ns.database_url)
    with Session(engine) as session, session.begin():
        digest = (
            rebuild(session, ns.projection)
            if ns.action == "rebuild"
            else snapshot_hash(session, ns.projection)
        )
    print(f"{ns.projection} {ns.action}: {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
