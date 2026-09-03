"""Decision to Task provenance (P6-09, development plan §7F).

Each action item of a Decision becomes one Task created through the normal command bus, so §7D.1
acceptance criteria are mandatory and the Task follows the usual lifecycle. Both directions of the
link are stored: ``decision_tasks`` maps the Decision to its Tasks, and each created Task carries
the Decision in its creation Event payload.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


def link(
    session: Session,
    *,
    decision_id: str,
    task_id: str,
    action_item: str,
    item_index: int,
    now: dt.datetime,
) -> None:
    session.execute(
        text(
            "INSERT INTO decision_tasks (decision_id, task_id, action_item, item_index, "
            "created_at) VALUES (:d, :t, :a, :i, :n) ON CONFLICT DO NOTHING"
        ),
        {"d": decision_id, "t": task_id, "a": action_item, "i": item_index, "n": now},
    )


def mark_taskified(session: Session, decision_id: str) -> None:
    session.execute(
        text(
            "UPDATE brainstorm_decisions SET status = 'taskified' "
            "WHERE decision_id = :d AND status = 'recorded'"
        ),
        {"d": decision_id},
    )


def provenance(session: Session, task_id: str) -> dict[str, Any] | None:
    """The Decision (and its session) a Task came from, for the document provenance section."""
    row = (
        session.execute(
            text(
                "SELECT dt.decision_id, dt.action_item, dt.item_index, d.brainstorm_id, "
                "d.statement FROM decision_tasks dt "
                "JOIN brainstorm_decisions d ON d.decision_id = dt.decision_id "
                "WHERE dt.task_id = :t"
            ),
            {"t": task_id},
        )
        .mappings()
        .first()
    )
    return None if row is None else dict(row)
