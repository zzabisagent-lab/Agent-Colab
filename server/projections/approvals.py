"""Approvals projection (display only; development plan §6.7). Rebuilt from APPROVAL_* Events and
the append-only decision ledger; ``used_count`` is derived from APPROVAL_CONSUMED Events."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.events.canonical import canonical_json
from server.events.postgres_store import _COLUMNS, row_to_event

NAME = "approvals"


def apply(session: Session, event: dict[str, Any]) -> None:
    p = event["payload"]
    t = event["type"]
    aid = event["aggregate_id"]
    if t == "APPROVAL_REQUESTED":
        session.execute(
            text(
                "INSERT INTO approvals_projection (approval_id, workspace_id, subject_type, "
                "subject_id, "
                "action, risk, status, used_count, max_uses, expires_at, requested_by, decided_by, "
                "last_event_id, updated_at) VALUES (:a, :ws, :st, :si, :ac, :r, 'PENDING', 0, "
                ":m, :e, "
                ":rb, '[]'::jsonb, :ev, :now) ON CONFLICT (approval_id) DO NOTHING"
            ),
            {
                "a": aid,
                "ws": event["workspace_id"],
                "st": p["subject_type"],
                "si": p["subject_id"],
                "ac": p["action"],
                "r": p["risk"],
                "m": p.get("max_uses"),
                "e": p["expires_at"],
                "rb": event["actor_account_id"],
                "ev": event["event_id"],
                "now": event["occurred_at"],
            },
        )
        return
    status = {
        "APPROVAL_GRANTED": "APPROVED",
        "APPROVAL_REJECTED": "REJECTED",
        "APPROVAL_CANCELLED": "CANCELLED",
        "APPROVAL_EXPIRED": "EXPIRED",
        "APPROVAL_REVOKED": "REVOKED",
    }.get(t)
    deciders = [
        str(r[0])
        for r in session.execute(
            text("SELECT decided_by FROM approval_decisions WHERE approval_id = :a ORDER BY id"),
            {"a": aid},
        ).all()
    ]
    if t == "APPROVAL_CONSUMED":
        used = int(p["used_count"])
        session.execute(
            text(
                "UPDATE approvals_projection SET used_count = :u, status = CASE WHEN max_uses "
                "IS NOT NULL AND :u >= max_uses THEN 'CONSUMED' ELSE 'PARTIALLY_CONSUMED' END, "
                "decided_by = CAST(:d AS jsonb), last_event_id = :ev, updated_at = :now "
                "WHERE approval_id = :a"
            ),
            {
                "u": used,
                "d": json.dumps(deciders),
                "ev": event["event_id"],
                "now": event["occurred_at"],
                "a": aid,
            },
        )
    elif t == "APPROVAL_ESCALATED":
        session.execute(
            text(
                "UPDATE approvals_projection SET last_event_id = :ev, updated_at = :now "
                "WHERE approval_id = :a"
            ),
            {"ev": event["event_id"], "now": event["occurred_at"], "a": aid},
        )
    elif status:
        session.execute(
            text(
                "UPDATE approvals_projection SET status = :s, decided_by = CAST(:d AS jsonb), "
                "last_event_id = :ev, updated_at = :now WHERE approval_id = :a"
            ),
            {
                "s": status,
                "d": json.dumps(deciders),
                "ev": event["event_id"],
                "now": event["occurred_at"],
                "a": aid,
            },
        )


def rebuild_approvals(session: Session, workspace_id: str | None = None) -> int:
    """Delete the projection and replay every approval Event in recorded order; returns rows."""
    session.execute(
        text(
            "DELETE FROM approvals_projection "
            "WHERE CAST(:ws AS uuid) IS NULL OR workspace_id = CAST(:ws AS uuid)"
        ),
        {"ws": workspace_id},
    )
    rows = session.execute(
        text(
            f"SELECT {_COLUMNS} FROM events WHERE aggregate_type = 'approval' "  # noqa: S608
            "AND (CAST(:ws AS uuid) IS NULL OR workspace_id = CAST(:ws AS uuid)) "
            "ORDER BY recorded_seq"
        ),
        {"ws": workspace_id},
    ).mappings()
    n = 0
    for row in rows:
        apply(session, row_to_event(row))
        n += 1
    session.execute(
        text(
            "INSERT INTO projection_checkpoints (projection, last_recorded_seq, snapshot_hash, "
            "updated_at) VALUES (:p, COALESCE((SELECT max(recorded_seq) FROM events "
            "WHERE aggregate_type = 'approval'), 0), :h, now()) ON CONFLICT (projection) DO UPDATE "
            "SET last_recorded_seq = EXCLUDED.last_recorded_seq, snapshot_hash = "
            "EXCLUDED.snapshot_hash, updated_at = now()"
        ),
        {"p": NAME, "h": snapshot_hash(session)},
    )
    return n


def snapshot_hash(session: Session, workspace_id: str | None = None) -> str:
    """SHA-256 of the canonical JSON of every deterministic projection column (no timestamps)."""
    rows = session.execute(
        text(
            "SELECT approval_id, workspace_id::text, subject_type, subject_id, action, risk, "
            "status, used_count, max_uses, requested_by::text, decided_by, last_event_id "
            "FROM approvals_projection "
            "WHERE CAST(:ws AS uuid) IS NULL OR workspace_id = CAST(:ws AS uuid) "
            "ORDER BY approval_id"
        ),
        {"ws": workspace_id},
    ).all()
    data = [list(r) for r in rows]
    return hashlib.sha256(canonical_json(data)).hexdigest()
