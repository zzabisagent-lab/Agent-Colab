"""Turn delivery and recording (P6-02, development plan §7F).

The server hands each participating Agent a ``brainstorm_turn`` work item carrying the transcript
reference, the remaining turns and the expected contribution type; Agents answer through the MCP
tool ``brainstorm_contribute``. Human participants speak freely and a plain utterance is recorded
as ``IDEA``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.brainstorm import engine as eng

TURN_RESULT_SCHEMA = "colab.work-result.v1"
DEFAULT_TURN_DEADLINE_S = 600


def new_turn_id() -> str:
    return "bst-" + uuid.uuid4().hex[:20]


def remaining_turns(state: eng.BrainstormState, participant: eng.Participant) -> int:
    """Turns this participant may still take before its own or the session limit stops it."""
    per_agent = state.limits.turns_per_agent
    left_for_agent = max(per_agent - participant.turns_taken, 0) if per_agent else 1 << 30
    total = state.limits.total_turns
    left_total = max(total - state.turn_no, 0) if total else 1 << 30
    return int(min(left_for_agent, left_total))


def turn_payload(
    state: eng.BrainstormState,
    participant: eng.Participant,
    *,
    expected_type: str = "IDEA",
) -> dict[str, Any]:
    return {
        "brainstorm_id": state.brainstorm_id,
        "topic": state.topic,
        "channel_id": state.channel_id,
        "transcript_ref": f"colab://brainstorm/{state.brainstorm_id}/transcript",
        "turn_no": state.turn_no + 1,
        "remaining_turns": remaining_turns(state, participant),
        "expected_contribution_type": expected_type,
        "contribution_types": list(eng.CONTRIBUTION_TYPES),
    }


def record(
    session: Session,
    state: eng.BrainstormState,
    *,
    account_uuid: uuid.UUID,
    contribution_type: str,
    body: str,
    event_id: str,
    work_item_id: str | None,
    now: dt.datetime,
) -> str:
    """Insert the turn row; the caller has already appended ``IDEA_RECORDED``."""
    turn_id = new_turn_id()
    session.execute(
        text(
            "INSERT INTO brainstorm_turns (turn_id, brainstorm_id, seq, account_id, "
            "contribution_type, body, work_item_id, event_id, created_at) "
            "VALUES (:t, :b, :s, :a, :c, :body, :w, :e, :n)"
        ),
        {
            "t": turn_id,
            "b": state.brainstorm_id,
            "s": state.turn_no + 1,
            "a": account_uuid,
            "c": contribution_type,
            "body": body,
            "w": work_item_id,
            "e": event_id,
            "n": now,
        },
    )
    return turn_id
