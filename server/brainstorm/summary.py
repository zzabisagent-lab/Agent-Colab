"""Summary drafts and facilitator approval (P6-09, development plan §7F).

``summarize`` prefers an Agent that holds ``brainstorm.summarize`` and is **not** a participant,
falling back to the best-scored participant. The draft is recorded as ``DRAFT`` and is never
posted to the channel until the facilitator approves it (V-P6-27).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.agents import routing
from server.brainstorm import engine as eng

SUMMARIZE_PERMISSION = "brainstorm.summarize"


class SummaryError(ValueError):
    def __init__(self, code: str, detail: str = "", status: int = 409) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = status


def new_summary_id() -> str:
    return "bsum-" + uuid.uuid4().hex[:20]


def choose_summarizer(
    session: Session,
    state: eng.BrainstormState,
    *,
    workspace_id: str,
    authorizer: Any,
    correlation_id: str = "-",
) -> tuple[routing.Candidate | None, bool]:
    """(candidate, is_participant). Non-participants win; ties break by ascending agent_id."""
    participants = [str(p.account_uuid) for p in state.participants]
    outsiders = routing.candidates(
        session,
        workspace_id=workspace_id,
        channel_uuid=str(state.channel_uuid),
        required_capability=None,
        domain=None,
        authorizer=authorizer,
        exclude_accounts=participants,
        permission=SUMMARIZE_PERMISSION,
        correlation_id=correlation_id,
    )
    if outsiders:
        return outsiders[0], False
    inside = routing.candidates(
        session,
        workspace_id=workspace_id,
        channel_uuid=str(state.channel_uuid),
        required_capability=None,
        domain=None,
        authorizer=authorizer,
        permission=SUMMARIZE_PERMISSION,
        correlation_id=correlation_id,
    )
    allowed = {str(p.account_uuid) for p in state.participants}
    inside = [c for c in inside if c.account_uuid in allowed]
    return (inside[0], True) if inside else (None, False)


def draft_body(state: eng.BrainstormState, transcript: list[dict[str, Any]]) -> str:
    """A deterministic skeleton summary: no LLM, so the same transcript yields the same bytes."""
    by_type: dict[str, list[dict[str, Any]]] = {t: [] for t in eng.CONTRIBUTION_TYPES}
    for turn in transcript:
        by_type.setdefault(str(turn["contribution_type"]), []).append(turn)
    lines = [f"# Brainstorm summary: {state.topic}", "", f"Session `{state.brainstorm_id}`.", ""]
    headings = {
        "IDEA": "Ideas and arguments",
        "CHALLENGE": "Challenges and alternatives",
        "QUESTION": "Open questions",
        "GUIDANCE": "Guidance",
    }
    for kind, heading in headings.items():
        lines.append(f"## {heading}")
        lines.append("")
        entries = by_type.get(kind, [])
        if not entries:
            lines.append("_none recorded_")
        else:
            lines.extend(
                f"- {t['account_id']}: {t['body']} [[evt:{t['event_id']}]]" for t in entries
            )
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def insert(
    session: Session,
    *,
    summary_id: str,
    brainstorm_id: str,
    author: uuid.UUID,
    body: str,
    artifact_id: str | None,
    event_id: str | None,
    now: dt.datetime,
) -> None:
    session.execute(
        text(
            "INSERT INTO brainstorm_summaries (summary_id, brainstorm_id, author_account_id, "
            "status, body, artifact_id, event_id, created_at) "
            "VALUES (:s, :b, :a, 'DRAFT', :body, :art, :e, :n)"
        ),
        {
            "s": summary_id,
            "b": brainstorm_id,
            "a": author,
            "body": body,
            "art": artifact_id,
            "e": event_id,
            "n": now,
        },
    )


def approve(
    session: Session, summary_id: str, *, approver: uuid.UUID, now: dt.datetime, posted: bool
) -> None:
    session.execute(
        text(
            "UPDATE brainstorm_summaries SET status = 'APPROVED', approved_by = :a, "
            "approved_at = :n, posted_at = CASE WHEN :p THEN :n ELSE posted_at END "
            "WHERE summary_id = :s AND status = 'DRAFT'"
        ),
        {"a": approver, "n": now, "p": posted, "s": summary_id},
    )


def load(session: Session, summary_id: str) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                "SELECT summary_id, brainstorm_id, author_account_id, status, body, artifact_id, "
                "posted_at, approved_by, approved_at, event_id, created_at "
                "FROM brainstorm_summaries WHERE summary_id = :s"
            ),
            {"s": summary_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    data = dict(row)
    data["author_account_id"] = str(data["author_account_id"])
    data["approved_by"] = None if data["approved_by"] is None else str(data["approved_by"])
    return data


def list_for(session: Session, brainstorm_id: str) -> list[dict[str, Any]]:
    rows = (
        session.execute(
            text(
                "SELECT summary_id, status, author_account_id, artifact_id, "
                "created_at, approved_at, "
                "posted_at FROM brainstorm_summaries WHERE brainstorm_id = :b ORDER BY created_at"
            ),
            {"b": brainstorm_id},
        )
        .mappings()
        .all()
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["author_account_id"] = str(item["author_account_id"])
        out.append(item)
    return out
