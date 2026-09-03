"""Brainstorm session state: rows, participants, round-robin order and status (P6-02).

The session aggregate lives in the Event stream (``bs-...``); these helpers keep the projection
the engine reads to decide whose turn it is and whether a limit was breached. Every write happens
inside the caller's command transaction.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.brainstorm.limits import Limits, parse

STATUSES: tuple[str, ...] = ("OPEN", "PAUSED", "CLOSED")
CONTRIBUTION_TYPES: tuple[str, ...] = ("IDEA", "CHALLENGE", "QUESTION", "GUIDANCE")


class EngineError(ValueError):
    def __init__(self, code: str, detail: str = "", status: int = 409) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = status


@dataclass(frozen=True)
class Participant:
    account_uuid: uuid.UUID
    account_id: str
    role: str  # human | agent
    agent_id: str | None
    seat: int
    turns_taken: int


@dataclass(frozen=True)
class BrainstormState:
    """One Brainstorm row plus its participants."""

    id: uuid.UUID
    brainstorm_id: str
    workspace_id: uuid.UUID
    channel_uuid: uuid.UUID
    channel_id: str
    topic: str
    facilitator_uuid: uuid.UUID
    status: str
    limits: Limits
    turn_no: int
    turn_index: int
    last_contributor: uuid.UUID | None
    consecutive_turns: int
    paused_reason: str | None
    started_at: dt.datetime
    closed_at: dt.datetime | None
    participants: tuple[Participant, ...]

    @property
    def agents(self) -> tuple[Participant, ...]:
        return tuple(p for p in self.participants if p.role == "agent")

    def participant(self, account_uuid: uuid.UUID) -> Participant | None:
        return next((p for p in self.participants if p.account_uuid == account_uuid), None)

    def view(self) -> dict[str, Any]:
        return {
            "brainstorm_id": self.brainstorm_id,
            "channel_id": self.channel_id,
            "topic": self.topic,
            "status": self.status,
            "limits": self.limits.as_dict(),
            "turn_no": self.turn_no,
            "paused_reason": self.paused_reason,
            "started_at": iso(self.started_at),
            "closed_at": iso(self.closed_at),
            "facilitator_account_id": str(self.facilitator_uuid),
            "participants": [
                {
                    "account_id": p.account_id,
                    "role": p.role,
                    "agent_id": p.agent_id,
                    "seat": p.seat,
                    "turns_taken": p.turns_taken,
                }
                for p in self.participants
            ],
            "next_agent_account_id": (n.account_id if (n := next_agent(self)) else None),
        }


def iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return (
        value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{value.microsecond // 1000:03d}Z"
    )


def new_brainstorm_id() -> str:
    return "bs-" + uuid.uuid4().hex[:20]


def load(session: Session, workspace_id: uuid.UUID, brainstorm_id: str) -> BrainstormState | None:
    row = (
        session.execute(
            text(
                "SELECT b.id, b.brainstorm_id, b.workspace_id, b.channel_id, c.channel_id AS chan, "
                "b.topic, b.facilitator_account_id, b.status, b.limits, b.turn_no, b.turn_index, "
                "b.last_contributor_account_id, b.consecutive_turns, b.paused_reason, "
                "b.started_at, "
                "b.closed_at FROM brainstorms b JOIN channels c ON c.id = b.channel_id "
                "WHERE b.workspace_id = :w AND b.brainstorm_id = :b"
            ),
            {"w": workspace_id, "b": brainstorm_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    parts = (
        session.execute(
            text(
                "SELECT p.account_id, a.account_id AS public_id, p.role, p.agent_id, p.seat, "
                "p.turns_taken FROM brainstorm_participants p JOIN accounts a ON a.id "
                "= p.account_id "
                "WHERE p.brainstorm_id = :b ORDER BY p.seat"
            ),
            {"b": brainstorm_id},
        )
        .mappings()
        .all()
    )
    return BrainstormState(
        id=row["id"],
        brainstorm_id=row["brainstorm_id"],
        workspace_id=row["workspace_id"],
        channel_uuid=row["channel_id"],
        channel_id=row["chan"],
        topic=row["topic"],
        facilitator_uuid=row["facilitator_account_id"],
        status=row["status"],
        limits=parse(row["limits"]),
        turn_no=int(row["turn_no"]),
        turn_index=int(row["turn_index"]),
        last_contributor=row["last_contributor_account_id"],
        consecutive_turns=int(row["consecutive_turns"]),
        paused_reason=row["paused_reason"],
        started_at=row["started_at"],
        closed_at=row["closed_at"],
        participants=tuple(
            Participant(
                account_uuid=p["account_id"],
                account_id=p["public_id"],
                role=p["role"],
                agent_id=p["agent_id"],
                seat=int(p["seat"]),
                turns_taken=int(p["turns_taken"]),
            )
            for p in parts
        ),
    )


def require(session: Session, workspace_id: uuid.UUID, brainstorm_id: str) -> BrainstormState:
    found = load(session, workspace_id, brainstorm_id)
    if found is None:
        raise EngineError("BRAINSTORM_NOT_FOUND", brainstorm_id, status=404)
    return found


def list_sessions(
    session: Session, workspace_id: uuid.UUID, *, status: str | None = None
) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            "SELECT brainstorm_id FROM brainstorms WHERE workspace_id = :w "
            "AND (CAST(:s AS text) IS NULL OR status = CAST(:s AS text)) ORDER BY brainstorm_id"
        ),
        {"w": workspace_id, "s": status},
    ).all()
    return [require(session, workspace_id, str(r[0])).view() for r in rows]


def insert(
    session: Session,
    *,
    brainstorm_id: str,
    workspace_id: uuid.UUID,
    channel_uuid: uuid.UUID,
    topic: str,
    facilitator: uuid.UUID,
    limits: Limits,
    now: dt.datetime,
) -> uuid.UUID:
    row_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO brainstorms (id, brainstorm_id, workspace_id, channel_id, topic, "
            "facilitator_account_id, status, limits, started_at, created_at, updated_at) "
            "VALUES (:i, :b, :w, :c, :t, :f, 'OPEN', CAST(:l AS jsonb), :n, :n, :n)"
        ),
        {
            "i": row_id,
            "b": brainstorm_id,
            "w": workspace_id,
            "c": channel_uuid,
            "t": topic,
            "f": facilitator,
            "l": dump_json(limits.as_dict()),
            "n": now,
        },
    )
    return row_id


def add_participant(
    session: Session,
    *,
    brainstorm_id: str,
    account_uuid: uuid.UUID,
    role: str,
    agent_id: str | None,
    now: dt.datetime,
) -> int:
    """Seat a participant deterministically; returns the seat (idempotent per account)."""
    seat = session.execute(
        text(
            "SELECT coalesce(max(seat), -1) + 1 FROM brainstorm_participants WHERE "
            "brainstorm_id = :b"
        ),
        {"b": brainstorm_id},
    ).scalar_one()
    session.execute(
        text(
            "INSERT INTO brainstorm_participants (brainstorm_id, account_id, role, agent_id, "
            "seat, joined_at) VALUES (:b, :a, :r, :g, :s, :n) ON CONFLICT DO NOTHING"
        ),
        {"b": brainstorm_id, "a": account_uuid, "r": role, "g": agent_id, "s": seat, "n": now},
    )
    return int(seat)


def next_agent(state: BrainstormState) -> Participant | None:
    """Round-robin over agent seats: reproducible from ``turn_index`` alone (V-P6-26)."""
    agents = state.agents
    if not agents:
        return None
    return agents[state.turn_index % len(agents)]


def advance(
    session: Session,
    state: BrainstormState,
    *,
    contributor: uuid.UUID,
    is_agent: bool,
    now: dt.datetime,
) -> None:
    """Record one accepted turn: counters, consecutive tracking and the round-robin cursor."""
    consecutive = state.consecutive_turns + 1 if state.last_contributor == contributor else 1
    turn_index = state.turn_index + 1 if is_agent else state.turn_index
    session.execute(
        text(
            "UPDATE brainstorms SET turn_no = turn_no + 1, turn_index = :x, "
            "last_contributor_account_id = :a, consecutive_turns = :k, updated_at = :n "
            "WHERE brainstorm_id = :b"
        ),
        {"x": turn_index, "a": contributor, "k": consecutive, "n": now, "b": state.brainstorm_id},
    )
    session.execute(
        text(
            "UPDATE brainstorm_participants SET turns_taken = turns_taken + 1 "
            "WHERE brainstorm_id = :b AND account_id = :a"
        ),
        {"b": state.brainstorm_id, "a": contributor},
    )


def set_status(
    session: Session,
    brainstorm_id: str,
    status: str,
    *,
    now: dt.datetime,
    reason: str | None = None,
    event_id: str | None = None,
) -> None:
    session.execute(
        text(
            "UPDATE brainstorms SET status = :s, paused_reason = :r, updated_at = :n, "
            "closed_at = CASE WHEN :s = 'CLOSED' THEN :n ELSE closed_at END, "
            "last_event_id = coalesce(:e, last_event_id) WHERE brainstorm_id = :b"
        ),
        {"s": status, "r": reason, "n": now, "e": event_id, "b": brainstorm_id},
    )


def spent_cost_units(session: Session, brainstorm_id: str) -> int:
    return int(
        session.execute(
            text("SELECT coalesce(sum(cost_units), 0) FROM usage_records WHERE brainstorm_id = :b"),
            {"b": brainstorm_id},
        ).scalar_one()
    )


def transcript(session: Session, brainstorm_id: str) -> list[dict[str, Any]]:
    rows = (
        session.execute(
            text(
                "SELECT t.seq, a.account_id, t.contribution_type, t.body, t.event_id, t.created_at "
                "FROM brainstorm_turns t JOIN accounts a ON a.id = t.account_id "
                "WHERE t.brainstorm_id = :b ORDER BY t.seq"
            ),
            {"b": brainstorm_id},
        )
        .mappings()
        .all()
    )
    return [
        {
            "seq": int(r["seq"]),
            "account_id": r["account_id"],
            "contribution_type": r["contribution_type"],
            "body": r["body"],
            "event_id": r["event_id"],
            "at": iso(r["created_at"]),
        }
        for r in rows
    ]


def dump_json(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True)
