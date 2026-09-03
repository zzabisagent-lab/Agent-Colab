"""Decision records (P6-09, development plan §7F).

The facilitator records the Decision; an optional ``--vote`` tally of participating Humans'
reactions may be attached but never decides. Action items travel with the Decision so that
taskify can create one Task per item with mandatory acceptance criteria (§7D.1).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


class DecisionError(ValueError):
    def __init__(self, code: str, detail: str = "", status: int = 400) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = status


def new_decision_id() -> str:
    return "dec-" + uuid.uuid4().hex[:20]


def validate_action_items(raw: Any) -> list[dict[str, Any]]:
    """Each item needs a statement and at least one required acceptance criterion (§7D.1)."""
    if raw in (None, ()):
        return []
    if not isinstance(raw, list | tuple):
        raise DecisionError("DECISION_ACTION_ITEMS_INVALID", "action_items must be a list")
    items: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not str(item.get("statement", "")).strip():
            raise DecisionError("DECISION_ACTION_ITEMS_INVALID", f"item {index} needs a statement")
        criteria = item.get("criteria") or []
        if not isinstance(criteria, list | tuple) or not criteria:
            raise DecisionError(
                "DECISION_ACTION_ITEM_CRITERIA_REQUIRED",
                f"item {index} ({item['statement']!r}) needs acceptance criteria",
            )
        normalized: list[dict[str, Any]] = []
        for criterion in criteria:
            if isinstance(criterion, str):
                normalized.append(
                    {"statement": criterion, "check_type": "evidence", "required": True}
                )
                continue
            if not isinstance(criterion, dict) or not str(criterion.get("statement", "")).strip():
                raise DecisionError(
                    "DECISION_ACTION_ITEM_CRITERIA_REQUIRED", f"item {index} criterion invalid"
                )
            normalized.append(
                {
                    "statement": str(criterion["statement"]),
                    "check_type": str(criterion.get("check_type", "evidence")),
                    "required": bool(criterion.get("required", True)),
                }
            )
        items.append({"statement": str(item["statement"]), "criteria": normalized})
    return items


def validate_vote(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise DecisionError("DECISION_VOTE_INVALID", "vote must be an object")
    up, down = raw.get("up", 0), raw.get("down", 0)
    for name, value in (("up", up), ("down", down)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DecisionError("DECISION_VOTE_INVALID", f"{name} must be a non-negative integer")
    voters = raw.get("voters", [])
    if not isinstance(voters, list | tuple):
        raise DecisionError("DECISION_VOTE_INVALID", "voters must be a list")
    return {"up": int(up), "down": int(down), "voters": [str(v) for v in voters]}


def insert(
    session: Session,
    *,
    decision_id: str,
    brainstorm_id: str,
    workspace_id: uuid.UUID,
    statement: str,
    rationale: str,
    source_event_ids: list[str],
    action_items: list[dict[str, Any]],
    vote: dict[str, Any] | None,
    decided_by: uuid.UUID,
    event_id: str,
    now: dt.datetime,
) -> None:
    session.execute(
        text(
            "INSERT INTO brainstorm_decisions (decision_id, brainstorm_id, workspace_id, "
            "statement, rationale, source_event_ids, action_items, vote, decided_by, decided_at, "
            "event_id) VALUES (:d, :b, :w, :s, :r, CAST(:src AS jsonb), CAST(:ai AS jsonb), "
            "CAST(:v AS jsonb), :by, :n, :e)"
        ),
        {
            "d": decision_id,
            "b": brainstorm_id,
            "w": workspace_id,
            "s": statement,
            "r": rationale,
            "src": json.dumps(source_event_ids),
            "ai": json.dumps(action_items),
            "v": None if vote is None else json.dumps(vote),
            "by": decided_by,
            "n": now,
            "e": event_id,
        },
    )


def load(session: Session, workspace_id: uuid.UUID, decision_id: str) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                "SELECT decision_id, brainstorm_id, statement, rationale, source_event_ids, "
                "action_items, vote, status, decided_by, decided_at, event_id "
                "FROM brainstorm_decisions WHERE workspace_id = :w AND decision_id = :d"
            ),
            {"w": workspace_id, "d": decision_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    data = dict(row)
    data["decided_by"] = str(data["decided_by"])
    data["tasks"] = tasks_of(session, decision_id)
    return data


def list_for(session: Session, brainstorm_id: str) -> list[dict[str, Any]]:
    rows = (
        session.execute(
            text(
                "SELECT decision_id, statement, rationale, status, action_items, vote, decided_at "
                "FROM brainstorm_decisions WHERE brainstorm_id = :b ORDER BY "
                "decided_at, decision_id"
            ),
            {"b": brainstorm_id},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def tasks_of(session: Session, decision_id: str) -> list[dict[str, Any]]:
    rows = (
        session.execute(
            text(
                "SELECT task_id, action_item, item_index FROM decision_tasks "
                "WHERE decision_id = :d ORDER BY item_index"
            ),
            {"d": decision_id},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def decision_of_task(session: Session, task_id: str) -> str | None:
    row = session.execute(
        text("SELECT decision_id FROM decision_tasks WHERE task_id = :t"), {"t": task_id}
    ).first()
    return None if row is None else str(row[0])
