"""Operations overview (P4-02): dependencies, Tasks, Agents, outbox, backups, maintenance."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.clock import Clock
from server.ops import probes


def _counts(session: Session, sql: str, params: dict[str, Any]) -> dict[str, int]:
    return {str(r[0]): int(r[1]) for r in session.execute(text(sql), params).all()}


def maintenance_state(session: Session) -> dict[str, Any]:
    """The P4-13 maintenance-mode status when that package is present; inactive otherwise."""
    try:
        from server.maintenance import mode as maintenance  # P4-13
    except ImportError:
        return {"active": False, "source": "unavailable"}
    status = maintenance.status(session)
    return {"active": bool(status.get("active", False)), **status}


def last_backup(session: Session) -> dict[str, Any] | None:
    row = session.execute(
        text(
            "SELECT backup_id, path, size_bytes, sha256, created_at, created_by, includes_ledger "
            "FROM backups ORDER BY created_at DESC LIMIT 1"
        )
    ).first()
    if row is None:
        return None
    return {
        "backup_id": str(row[0]),
        "path": str(row[1]),
        "size_bytes": int(row[2]),
        "sha256": str(row[3]),
        "created_at": row[4].isoformat(),
        "created_by": str(row[5]),
        "includes_ledger": bool(row[6]),
    }


def list_backups(session: Session, limit: int = 50) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            "SELECT backup_id, path, size_bytes, sha256, created_at, created_by, tool_version, "
            "includes_master_key, includes_ledger FROM backups ORDER BY created_at DESC LIMIT :l"
        ),
        {"l": limit},
    ).all()
    return [
        {
            "backup_id": str(r[0]),
            "path": str(r[1]),
            "size_bytes": int(r[2]),
            "sha256": str(r[3]),
            "created_at": r[4].isoformat(),
            "created_by": str(r[5]),
            "tool_version": str(r[6]),
            "includes_master_key": bool(r[7]),
            "includes_ledger": bool(r[8]),
        }
        for r in rows
    ]


def overview(
    session: Session, workspace_id: uuid.UUID, clock: Clock, *, refresh: bool = False
) -> dict[str, Any]:
    results = probes.run_probes(session, clock=clock, refresh=refresh)
    tasks = _counts(
        session,
        "SELECT status, count(*) FROM tasks_projection WHERE workspace_id = :w GROUP BY status",
        {"w": workspace_id},
    )
    agents = _counts(
        session,
        "SELECT status, count(*) FROM agents WHERE workspace_id = :w GROUP BY status",
        {"w": workspace_id},
    )
    online = int(
        session.execute(
            text("SELECT count(*) FROM agents WHERE workspace_id = :w AND online"),
            {"w": workspace_id},
        ).scalar_one()
    )
    outbox_rows = session.execute(
        text(
            "SELECT split_part(kind, '.', 1), status, count(*) FROM delivery_outbox "
            "WHERE workspace_id = :w AND status IN ('pending','failed','dead') "
            "GROUP BY split_part(kind, '.', 1), status"
        ),
        {"w": workspace_id},
    ).all()
    outbox: dict[str, dict[str, int]] = {}
    for kind, status, count in outbox_rows:
        outbox.setdefault(str(kind), {})[str(status)] = int(count)
    pending_hard_deletes = int(
        session.execute(
            text(
                "SELECT count(*) FROM hard_delete_requests WHERE workspace_id = :w "
                "AND status IN ('PENDING_APPROVAL','APPROVED_WAITING')"
            ),
            {"w": workspace_id},
        ).scalar_one()
    )
    return {
        "generated_at": clock.now().isoformat(),
        "dependencies": [probes.as_dict(r) for r in results],
        "alerts": probes.alerts(results),
        "tasks": {"by_status": tasks, "total": sum(tasks.values())},
        "agents": {"by_status": agents, "online": online, "total": sum(agents.values())},
        "outbox": outbox,
        "last_backup": last_backup(session),
        "maintenance": maintenance_state(session),
        "hard_delete_requests_pending": pending_hard_deletes,
    }
