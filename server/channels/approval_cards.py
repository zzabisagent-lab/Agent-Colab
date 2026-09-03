"""Approval cards in the work channel (P6-01; development plan §7E, §7A.3, spec §8.4).

An `APPROVAL_REQUESTED` Event posts one card into the approval's channel and later Events patch
it in place, mirroring the Task card rules of P2-11. Buttons are offered only for LOW and MEDIUM
risk: HIGH and CRITICAL decisions require MFA re-authentication in the web console (§7E), so the
card carries guidance instead of buttons. As with every card button, the callback re-checks the
decision server-side, so a button is a convenience and never an authority.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.channels.actions import HUMAN_ONLY_RISKS, attach_button_contexts
from server.channels.outbox import Delivery, card_post_id, enqueue_delivery
from server.channels.task_cards import ChannelTarget, channel_target

APPROVAL_EVENT_TYPES = frozenset(
    {
        "APPROVAL_REQUESTED",
        "APPROVAL_GRANTED",
        "APPROVAL_REJECTED",
        "APPROVAL_EXPIRED",
        "APPROVAL_REVOKED",
        "APPROVAL_CONSUMED",
    }
)
STATUS_LINE = {
    "PENDING": "⏳ awaiting decision",
    "APPROVED": "✅ approved",
    "PARTIALLY_CONSUMED": "✅ approved (partially consumed)",
    "CONSUMED": "✅ approved (consumed)",
    "REJECTED": "🚫 rejected",
    "EXPIRED": "⌛ expired",
    "REVOKED": "🚫 revoked",
}
WEB_GUIDANCE = (
    "{risk} risk: decide in the web console after MFA re-authentication "
    "(Approvals queue). Buttons cannot approve HIGH or CRITICAL actions."
)


@dataclass(frozen=True)
class ApprovalCard:
    approval_id: str
    action: str
    risk: str
    status: str
    subject_type: str
    subject_id: str
    expires_at: dt.datetime | None
    quorum_required: int
    quorum_current: int
    channel_uuid: str | None


def load_card(session: Session, approval_id: str) -> ApprovalCard | None:
    row = (
        session.execute(
            text(
                "SELECT approval_id, action, risk, status, subject_type, subject_id, expires_at, "
                "quorum_required, channel_id FROM approval_grants WHERE approval_id = :a"
            ),
            {"a": approval_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    decided = session.execute(
        text(
            "SELECT count(*) FROM approval_decisions WHERE approval_id = "
            ":a AND decision = 'APPROVE'"
        ),
        {"a": approval_id},
    ).scalar_one()
    return ApprovalCard(
        approval_id=str(row["approval_id"]),
        action=str(row["action"]),
        risk=str(row["risk"]),
        status=str(row["status"]),
        subject_type=str(row["subject_type"]),
        subject_id=str(row["subject_id"]),
        expires_at=row["expires_at"],
        quorum_required=int(row["quorum_required"]),
        quorum_current=int(decided),
        channel_uuid=None if row["channel_id"] is None else str(row["channel_id"]),
    )


def render_body(card: ApprovalCard) -> str:
    lines = [
        f"**Approval `{card.approval_id}`** · {card.action} · risk {card.risk}",
        f"Subject: {card.subject_type} `{card.subject_id}`",
        f"Status: {STATUS_LINE.get(card.status, card.status)}"
        + (
            f" · approvals {card.quorum_current}/{card.quorum_required}"
            if card.quorum_required > 1
            else ""
        ),
    ]
    if card.expires_at is not None:
        lines.append(f"Valid until: {card.expires_at.isoformat()}")
    if card.risk in HUMAN_ONLY_RISKS and card.status == "PENDING":
        lines.append(WEB_GUIDANCE.format(risk=card.risk))
    return "\n".join(lines)


def buttons_for(card: ApprovalCard) -> tuple[str, ...]:
    """LOW and MEDIUM approvals may be decided from the channel; HIGH and above never are."""
    if card.status != "PENDING" or card.risk in HUMAN_ONLY_RISKS:
        return ()
    return ("approve", "reject")


def render_approval_event(
    session: Session,
    *,
    workspace_id: str,
    actor_uuid: str,
    event: dict[str, Any],
    now: dt.datetime,
) -> list[str]:
    """Enqueue the approval card (or its patch) for one approval Event; returns dedupe keys."""
    if event["type"] not in APPROVAL_EVENT_TYPES:
        return []
    approval_id = str(event.get("approval_id") or event["aggregate_id"])
    card = load_card(session, approval_id)
    if card is None or card.channel_uuid is None:
        return []
    target: ChannelTarget | None = channel_target(session, card.channel_uuid)
    if target is None:
        return []
    destination = f"mattermost:{target.external_channel_id}"
    key = f"approvalcard:{target.provider_instance_id}:{approval_id}"
    props = attach_button_contexts(
        {
            "buttons": list(buttons_for(card)),
            "agent_colab": {"subject_type": "approval", "subject_id": approval_id},
        },
        subject_type="approval",
        subject_id=approval_id,
        now=now,
    )
    root = card_post_id(session, target.provider_instance_id, "approval", approval_id)
    payload: dict[str, Any] = {"message": render_body(card), "props": props}
    dedupe = key if root is None else f"{key}:{event['aggregate_seq']}:{event['event_id'][-8:]}"
    if root is not None:
        payload["post_id"] = root
    # The first Event posts the card and claims the subject's single `role = 'card'` row; later
    # Events patch that post in place and must not claim a second one (channel_posts_card_idx is
    # unique per provider/subject where role = 'card'), exactly as the Task card patch does.
    delivery = (
        Delivery(
            "mattermost.post",
            destination,
            payload,
            dedupe,
            subject_type="approval",
            subject_id=approval_id,
            role="card",
        )
        if root is None
        else Delivery("mattermost.patch", destination, payload, dedupe)
    )
    accepted = enqueue_delivery(
        session,
        workspace_id=workspace_id,
        source_event_id=event["event_id"],
        delivery=delivery,
        provider_instance_id=target.provider_instance_id,
        external_channel_id=target.external_channel_id,
        now=now,
    )
    return [dedupe] if accepted else []


def approval_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None
