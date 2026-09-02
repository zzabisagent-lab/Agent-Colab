"""Shared helpers for push delivery channels (webhook, Mattermost bot; development plan §7B).

``mark_delivered`` performs the same state change as a pull ``poll`` for one item: DELIVERED (or
redelivered), ``delivery_count += 1``, a ``delivery`` receipt and a ``WORK_ITEM_DELIVERED``
Event. It is idempotent per delivery number: a second call for the same generation returns the
existing receipt without a new Event (no duplicate side effects on retry, CS-09).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.agents.adapters.contract import WorkItemView
from server.domain.clock import Clock
from server.events.canonical import canonical_json
from server.events.store import EventStore
from server.work import inbox, receipts
from server.work.state import WorkItemState, transition


@dataclass(frozen=True)
class AgentRecord:
    agent_id: str
    account_uuid: str
    account_id: str
    workspace_uuid: str
    adapter_type: str
    status: str
    endpoint: dict[str, Any]
    credential_ref: str | None
    display_name: str


def load_agent(session: Session, agent_id: str) -> AgentRecord | None:
    row = session.execute(
        text(
            "SELECT g.agent_id, g.account_id, a.account_id, g.workspace_id, g.adapter_type, "
            "g.status, g.endpoint, g.credential_ref, g.display_name FROM agents g "
            "JOIN accounts a ON a.id = g.account_id WHERE g.agent_id = :g"
        ),
        {"g": agent_id},
    ).first()
    if row is None:
        return None
    endpoint = row[6] if isinstance(row[6], dict) else {}
    return AgentRecord(
        agent_id=str(row[0]),
        account_uuid=str(row[1]),
        account_id=str(row[2]),
        workspace_uuid=str(row[3]),
        adapter_type=str(row[4]),
        status=str(row[5]),
        endpoint=dict(endpoint),
        credential_ref=row[7],
        display_name=str(row[8]),
    )


def view_of(item: inbox.WorkItem, delivery_no: int) -> WorkItemView:
    """Transport-neutral view; the payload body itself is fetched via ``payload_ref``."""
    return WorkItemView(
        work_item_id=item.work_item_id,
        kind=item.kind,
        agent_id=item.agent_id,
        task_id=item.task_id,
        correlation_id=item.correlation_id,
        deadline=item.deadline,
        payload_ref=item.payload_ref(),
        secret_handles=tuple(item.secret_handles),
        expected_result_schema=item.expected_result_schema,
        idempotency_key=item.idempotency_key,
        payload={
            "delivery_no": delivery_no,
            "payload_size_bytes": len(canonical_json(item.payload)),
            "brainstorm_id": item.brainstorm_id,
        },
    )


def delivery_receipt_for(session: Session, work_item_id: str, delivery_no: int) -> Any | None:
    return session.execute(
        text(
            "SELECT id, detail FROM work_item_receipts WHERE work_item_id = :w "
            "AND receipt_kind = 'delivery' AND delivery_no = :n"
        ),
        {"w": work_item_id, "n": delivery_no},
    ).first()


def mark_delivered(
    session: Session,
    store: EventStore,
    work_item_id: str,
    *,
    actor_account_id: str,
    clock: Clock,
    detail: dict[str, Any] | None = None,
) -> tuple[inbox.WorkItem, int, bool]:
    """DELIVERED + receipt + Event for one QUEUED item; returns (item, delivery_no, changed)."""
    item = inbox.load(session, work_item_id, for_update=True)
    if item.status is not WorkItemState.QUEUED:
        return item, item.delivery_count, False
    action = "redeliver" if item.delivery_count > 0 else "deliver"
    base = WorkItemState.DELIVERED if action == "redeliver" else item.status
    new_state = transition(base, action)
    delivery_no = item.delivery_count + 1
    now = clock.now()
    inbox._set(
        session,
        work_item_id,
        now,
        status=new_state.value,
        delivery_count=delivery_no,
        delivered_at=now,
    )
    receipts.record_receipt(
        session, work_item_id, "delivery", delivery_no=delivery_no, detail=detail
    )
    inbox._append(
        store,
        item,
        event_type="WORK_ITEM_DELIVERED",
        actor_account_id=actor_account_id,
        operation="deliver",
        key=f"{work_item_id}:{delivery_no}",
        payload={"work_item_id": work_item_id, "delivery_no": delivery_no},
    )
    return inbox.load(session, work_item_id), delivery_no, True


def workspace_uuid_of(item: inbox.WorkItem) -> uuid.UUID:
    return uuid.UUID(item.workspace_id)
