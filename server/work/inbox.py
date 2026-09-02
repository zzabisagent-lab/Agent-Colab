"""Durable work item inbox (development plan §7B.1, §7B.3, §7B.4; P1-12).

Every piece of work given to an Agent is a ``work_items`` row plus ``WORK_ITEM_*`` Events; chat
messages are never the only delivery path. Semantics:

- ``enqueue``   QUEUED (+ ``WORK_ITEM_QUEUED``), idempotent on ``idempotency_key``.
- ``poll``      pull delivery: QUEUED items (first delivery, or a redelivery re-queued by the
                timeout sweep) become DELIVERED with ``delivery_count += 1`` and a ``delivery``
                receipt + ``WORK_ITEM_DELIVERED``; un-acked DELIVERED items are returned again on
                every poll (reconnect semantics) without counting a new delivery.
- ``ack``       DELIVERED → ACKED (idempotent); ``start`` ACKED → IN_PROGRESS; ``reject``;
                ``cancel``.
- ``result``    exactly once per work item: the single ``result`` receipt is protected by a
                partial unique index; a second submission leaves a ``duplicate_result`` receipt,
                an audit row, and returns ``DUPLICATE_RESULT_IGNORED`` with zero state change.

A QUEUED row with ``delivery_count > 0`` is an item awaiting redelivery (re-queued by
``server.work.timeouts.sweep`` after the 60-second ack timeout).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.clock import Clock, isoformat_utc
from server.events.canonical import canonical_json
from server.events.store import AppendRequest, AppendResult, EventStore, EventStoreError
from server.observability.audit import append_audit
from server.work import receipts
from server.work.schemas import AdapterSchemaError, validate
from server.work.state import (
    REJECTION_CODES,
    TERMINAL_STATES,
    WorkItemError,
    WorkItemState,
    transition,
)

KINDS = frozenset(
    {
        "task_assignment",
        "subtask_assignment",
        "invoke",
        "cancel",
        "brainstorm_turn",
        "verification_assignment",
    }
)

_COLUMNS = (
    "work_item_id, workspace_id, kind, agent_id, task_id, brainstorm_id, correlation_id, deadline, "
    "payload, secret_handles, expected_result_schema, idempotency_key, status, delivery_count, "
    "delivered_at, acked_at, accepted_at, finished_at, created_at, updated_at"
)


@dataclass(frozen=True)
class WorkItem:
    work_item_id: str
    workspace_id: str
    kind: str
    agent_id: str
    task_id: str | None
    brainstorm_id: str | None
    correlation_id: str
    deadline: dt.datetime
    payload: dict[str, Any]
    secret_handles: list[str]
    expected_result_schema: str
    idempotency_key: str
    status: WorkItemState
    delivery_count: int
    delivered_at: dt.datetime | None
    acked_at: dt.datetime | None
    accepted_at: dt.datetime | None
    finished_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime

    def payload_ref(self) -> str:
        return f"colab://work/{self.work_item_id}/payload"

    def to_delivery(self) -> dict[str, Any]:
        """The §7B.1 work item envelope handed to the Agent (payload fetched separately)."""
        return {
            "schema_id": "colab.work-item.v1",
            "work_item_id": self.work_item_id,
            "kind": self.kind,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "brainstorm_id": self.brainstorm_id,
            "correlation_id": self.correlation_id,
            "deadline": isoformat_utc(self.deadline),
            "payload_ref": self.payload_ref(),
            "secret_handles": list(self.secret_handles),
            "expected_result_schema": self.expected_result_schema,
            "idempotency_key": self.idempotency_key,
            "delivery_no": self.delivery_count,
        }


def _row_to_item(row: Any) -> WorkItem:
    r = dict(row)
    return WorkItem(
        work_item_id=r["work_item_id"],
        workspace_id=str(r["workspace_id"]),
        kind=r["kind"],
        agent_id=r["agent_id"],
        task_id=r["task_id"],
        brainstorm_id=r["brainstorm_id"],
        correlation_id=r["correlation_id"],
        deadline=r["deadline"],
        payload=r["payload"],
        secret_handles=list(r["secret_handles"]),
        expected_result_schema=r["expected_result_schema"],
        idempotency_key=r["idempotency_key"],
        status=WorkItemState(r["status"]),
        delivery_count=int(r["delivery_count"]),
        delivered_at=r["delivered_at"],
        acked_at=r["acked_at"],
        accepted_at=r["accepted_at"],
        finished_at=r["finished_at"],
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


def new_work_item_id() -> str:
    return "wi-" + uuid.uuid4().hex[:24]


def load(session: Session, work_item_id: str, *, for_update: bool = False) -> WorkItem:
    lock = " FOR UPDATE" if for_update else ""
    row = (
        session.execute(
            text(f"SELECT {_COLUMNS} FROM work_items WHERE work_item_id = :w{lock}"),  # noqa: S608
            {"w": work_item_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise WorkItemError("WORK_ITEM_NOT_FOUND", work_item_id)
    return _row_to_item(row)


def _require_owner(item: WorkItem, agent_id: str) -> None:
    if item.agent_id != agent_id:
        raise WorkItemError(
            "WORK_ITEM_NOT_OWNER", f"{item.work_item_id} is not owned by {agent_id}"
        )


def _append(
    store: EventStore,
    item: WorkItem,
    *,
    event_type: str,
    actor_account_id: str,
    operation: str,
    key: str,
    payload: dict[str, Any],
    caused_by: str | None = None,
) -> AppendResult:
    try:
        return store.append(
            AppendRequest(
                workspace_id=item.workspace_id,
                aggregate_type="work_item",
                aggregate_id=item.work_item_id,
                type=event_type,
                actor_account_id=actor_account_id,
                correlation_id=item.correlation_id,
                idempotency_scope=f"work_item:{operation}",
                idempotency_key=key,
                payload=payload,
                task_id=item.task_id,
                caused_by=caused_by,
            )
        )
    except EventStoreError as exc:
        raise WorkItemError(exc.code, exc.detail) from exc


def _set(session: Session, work_item_id: str, now: dt.datetime, **cols: Any) -> None:
    assignments = ", ".join(f"{k} = :{k}" for k in cols)
    session.execute(
        text(f"UPDATE work_items SET {assignments}, updated_at = :now WHERE work_item_id = :w"),  # noqa: S608
        {**cols, "now": now, "w": work_item_id},
    )


# ------------------------------------------------------------------------------- enqueue


def enqueue(
    session: Session,
    store: EventStore,
    *,
    workspace_id: str,
    kind: str,
    agent_id: str,
    payload: dict[str, Any],
    deadline: dt.datetime,
    expected_result_schema: str,
    correlation_id: str,
    idempotency_key: str,
    actor_account_id: str,
    clock: Clock,
    task_id: str | None = None,
    brainstorm_id: str | None = None,
    secret_handles: list[str] | None = None,
    work_item_id: str | None = None,
) -> WorkItem:
    """Create a QUEUED work item and its ``WORK_ITEM_QUEUED`` Event (idempotent on the key)."""
    if kind not in KINDS:
        raise WorkItemError("WORK_ITEM_KIND_INVALID", kind)
    if (kind == "brainstorm_turn") != (brainstorm_id is not None):
        raise WorkItemError("WORK_ITEM_KIND_INVALID", "brainstorm_turn requires brainstorm_id")
    existing = (
        session.execute(
            text(f"SELECT {_COLUMNS} FROM work_items WHERE idempotency_key = :k"),  # noqa: S608
            {"k": idempotency_key},
        )
        .mappings()
        .first()
    )
    if existing is not None:
        item = _row_to_item(existing)
        if item.agent_id != agent_id or item.kind != kind:
            raise WorkItemError("IDEMPOTENCY_CONFLICT", "same key with a different work item")
        return item
    payload_bytes = len(canonical_json(payload))
    if payload_bytes > 1_000_000:
        raise WorkItemError("WORK_ITEM_PAYLOAD_TOO_LARGE", f"{payload_bytes} bytes > 1 MB")
    now = clock.now()
    wid = work_item_id or new_work_item_id()
    session.execute(
        text(
            "INSERT INTO work_items (work_item_id, workspace_id, kind, agent_id, task_id, "
            "brainstorm_id, correlation_id, deadline, payload, secret_handles, "
            "expected_result_schema, idempotency_key, status, delivery_count, created_at, "
            "updated_at) VALUES (:w, :ws, :k, :a, :t, :b, :c, :d, CAST(:p AS jsonb), "
            "CAST(:s AS jsonb), :e, :i, 'QUEUED', 0, :now, :now)"
        ),
        {
            "w": wid,
            "ws": uuid.UUID(workspace_id),
            "k": kind,
            "a": agent_id,
            "t": task_id,
            "b": brainstorm_id,
            "c": correlation_id,
            "d": deadline,
            "p": json.dumps(payload),
            "s": json.dumps(list(secret_handles or [])),
            "e": expected_result_schema,
            "i": idempotency_key,
            "now": now,
        },
    )
    item = load(session, wid)
    _append(
        store,
        item,
        event_type="WORK_ITEM_QUEUED",
        actor_account_id=actor_account_id,
        operation="queue",
        key=idempotency_key,
        payload={
            "work_item_id": wid,
            "kind": kind,
            "agent_id": agent_id,
            "deadline": isoformat_utc(deadline),
            "task_id": task_id,
            "brainstorm_id": brainstorm_id,
        },
    )
    return item


# ---------------------------------------------------------------------------------- poll


@dataclass(frozen=True)
class PollResult:
    items: list[WorkItem]
    delivered_event_ids: list[str]


def poll(
    session: Session,
    store: EventStore,
    agent_id: str,
    *,
    actor_account_id: str,
    clock: Clock,
    max_items: int = 10,
) -> PollResult:
    """Pull delivery for one Agent.

    QUEUED rows (first delivery or a re-queued redelivery) are delivered now: ``delivery_count``
    is incremented, a ``delivery`` receipt and a ``WORK_ITEM_DELIVERED`` Event are recorded.
    DELIVERED rows that are not yet acked are returned again (reconnect redelivery, §7B.3) with
    their current ``delivery_count`` and without a new delivery record. Rows are locked with
    ``FOR UPDATE SKIP LOCKED`` so concurrent polls never deliver the same row twice.
    """
    limit = max(1, min(int(max_items), 100))
    rows = (
        session.execute(
            text(
                f"SELECT {_COLUMNS} FROM work_items WHERE agent_id = :a "  # noqa: S608
                "AND status IN ('QUEUED','DELIVERED') ORDER BY created_at, work_item_id "
                "LIMIT :lim FOR UPDATE SKIP LOCKED"
            ),
            {"a": agent_id, "lim": limit},
        )
        .mappings()
        .all()
    )
    now = clock.now()
    delivered: list[WorkItem] = []
    event_ids: list[str] = []
    for row in rows:
        item = _row_to_item(row)
        if item.status is WorkItemState.QUEUED:
            action = "redeliver" if item.delivery_count > 0 else "deliver"
            base_state = WorkItemState.DELIVERED if action == "redeliver" else item.status
            new_state = transition(base_state, action)
            delivery_no = item.delivery_count + 1
            _set(
                session,
                item.work_item_id,
                now,
                status=new_state.value,
                delivery_count=delivery_no,
                delivered_at=now,
            )
            receipts.record_receipt(session, item.work_item_id, "delivery", delivery_no=delivery_no)
            res = _append(
                store,
                item,
                event_type="WORK_ITEM_DELIVERED",
                actor_account_id=actor_account_id,
                operation="deliver",
                key=f"{item.work_item_id}:{delivery_no}",
                payload={"work_item_id": item.work_item_id, "delivery_no": delivery_no},
            )
            event_ids.append(res.event_id)
            item = load(session, item.work_item_id)
        delivered.append(item)
    return PollResult(delivered, event_ids)


# --------------------------------------------------------------------- ack / start / reject


def ack(
    session: Session,
    store: EventStore,
    work_item_id: str,
    agent_id: str,
    *,
    actor_account_id: str,
    clock: Clock,
) -> WorkItem:
    item = load(session, work_item_id, for_update=True)
    _require_owner(item, agent_id)
    if item.status in (WorkItemState.ACKED, WorkItemState.IN_PROGRESS):
        return item  # idempotent
    new_state = transition(item.status, "ack")
    now = clock.now()
    _set(session, work_item_id, now, status=new_state.value, acked_at=now)
    receipts.record_receipt(session, work_item_id, "ack", delivery_no=item.delivery_count)
    _append(
        store,
        item,
        event_type="WORK_ITEM_ACKED",
        actor_account_id=actor_account_id,
        operation="ack",
        key=work_item_id,
        payload={"work_item_id": work_item_id},
    )
    return load(session, work_item_id)


def start(
    session: Session,
    store: EventStore,
    work_item_id: str,
    agent_id: str,
    *,
    actor_account_id: str,
    clock: Clock,
) -> WorkItem:
    item = load(session, work_item_id, for_update=True)
    _require_owner(item, agent_id)
    if item.status is WorkItemState.IN_PROGRESS:
        return item
    new_state = transition(item.status, "start")
    now = clock.now()
    _set(session, work_item_id, now, status=new_state.value, accepted_at=now)
    receipts.record_receipt(session, work_item_id, "accept", delivery_no=item.delivery_count)
    _append(
        store,
        item,
        event_type="WORK_ITEM_STARTED",
        actor_account_id=actor_account_id,
        operation="start",
        key=work_item_id,
        payload={"work_item_id": work_item_id},
    )
    return load(session, work_item_id)


def reject(
    session: Session,
    store: EventStore,
    work_item_id: str,
    agent_id: str,
    reason_code: str,
    *,
    actor_account_id: str,
    clock: Clock,
) -> WorkItem:
    if reason_code not in REJECTION_CODES:
        raise WorkItemError("WORK_ITEM_REJECTION_CODE_INVALID", reason_code)
    item = load(session, work_item_id, for_update=True)
    _require_owner(item, agent_id)
    new_state = transition(item.status, "reject")
    now = clock.now()
    _set(session, work_item_id, now, status=new_state.value, finished_at=now)
    receipts.record_receipt(
        session,
        work_item_id,
        "reject",
        delivery_no=item.delivery_count,
        detail={"reason_code": reason_code},
    )
    _append(
        store,
        item,
        event_type="WORK_ITEM_REJECTED",
        actor_account_id=actor_account_id,
        operation="reject",
        key=work_item_id,
        payload={"work_item_id": work_item_id, "reason_code": reason_code},
    )
    return load(session, work_item_id)


def cancel(
    session: Session,
    store: EventStore,
    work_item_id: str,
    reason_code: str,
    *,
    actor_account_id: str,
    clock: Clock,
) -> WorkItem:
    item = load(session, work_item_id, for_update=True)
    new_state = transition(item.status, "cancel")
    now = clock.now()
    _set(session, work_item_id, now, status=new_state.value, finished_at=now)
    _append(
        store,
        item,
        event_type="WORK_ITEM_CANCELLED",
        actor_account_id=actor_account_id,
        operation="cancel",
        key=work_item_id,
        payload={"work_item_id": work_item_id, "reason_code": reason_code},
    )
    return load(session, work_item_id)


def expire(
    session: Session,
    store: EventStore,
    work_item_id: str,
    reason_code: str,
    *,
    actor_account_id: str,
    clock: Clock,
) -> WorkItem:
    item = load(session, work_item_id, for_update=True)
    new_state = transition(item.status, "expire")
    now = clock.now()
    _set(session, work_item_id, now, status=new_state.value, finished_at=now)
    _append(
        store,
        item,
        event_type="WORK_ITEM_EXPIRED",
        actor_account_id=actor_account_id,
        operation="expire",
        key=work_item_id,
        payload={"work_item_id": work_item_id, "reason_code": reason_code},
    )
    return load(session, work_item_id)


def requeue_for_redelivery(session: Session, work_item_id: str, *, clock: Clock) -> WorkItem:
    """Timeout sweep helper: a DELIVERED item without ack goes back to QUEUED for the next poll."""
    item = load(session, work_item_id, for_update=True)
    transition(item.status, "redeliver")  # only valid from DELIVERED
    _set(session, work_item_id, clock.now(), status=WorkItemState.QUEUED.value)
    return load(session, work_item_id)


# -------------------------------------------------------------------------------- result


@dataclass(frozen=True)
class ResultOutcome:
    code: str  # RESULT_ACCEPTED | DUPLICATE_RESULT_IGNORED
    work_item_id: str
    receipt_id: int
    result_ref: str
    event_id: str | None
    item: WorkItem


def result_ref_for(work_item_id: str, result: dict[str, Any]) -> tuple[str, str]:
    digest = hashlib.sha256(canonical_json(result)).hexdigest()
    return f"colab://work/{work_item_id}/result/{digest}", digest


def result(
    session: Session,
    store: EventStore,
    work_item_id: str,
    agent_id: str,
    payload: dict[str, Any],
    *,
    actor_account_id: str,
    clock: Clock,
) -> ResultOutcome:
    """Accept a work result exactly once (V-P1-29, CS-02/09).

    The result must validate against ``schemas/adapters/work-result.v1.schema.json`` and carry
    either ``usage`` or ``usage_unavailable``. A result on a DELIVERED item implies the ack
    (§7B.4: acceptance is explicit but receipt is implied by a result). A second result — same or
    different body — leaves a ``duplicate_result`` receipt and an audit row and changes nothing.
    """
    try:
        validate("work_result", payload)
    except AdapterSchemaError as exc:
        raise WorkItemError(exc.code, exc.detail) from exc
    if payload["work_item_id"] != work_item_id:
        raise WorkItemError("WORK_RESULT_SCHEMA_INVALID", "work_item_id mismatch")
    item = load(session, work_item_id, for_update=True)
    _require_owner(item, agent_id)
    ref, digest = result_ref_for(work_item_id, payload)
    existing = receipts.result_receipt_of(session, work_item_id)
    if existing is not None or item.status in TERMINAL_STATES:
        return _duplicate(session, item, existing, ref, digest, actor_account_id, agent_id)
    if item.status is WorkItemState.DELIVERED:
        ack(session, store, work_item_id, agent_id, actor_account_id=actor_account_id, clock=clock)
        item = load(session, work_item_id, for_update=True)
    new_state = transition(item.status, "result")
    try:
        receipt = receipts.record_receipt(
            session,
            work_item_id,
            "result",
            delivery_no=item.delivery_count,
            result_ref=ref,
            result_sha256=digest,
            usage=payload.get("usage"),
            detail={"status": payload["status"], "error_code": payload.get("error_code")},
        )
    except receipts.DuplicateResultError:
        return _duplicate(
            session,
            item,
            receipts.result_receipt_of(session, work_item_id),
            ref,
            digest,
            actor_account_id,
            agent_id,
        )
    now = clock.now()
    _set(session, work_item_id, now, status=new_state.value, finished_at=now)
    res = _append(
        store,
        item,
        event_type="WORK_ITEM_RESULT_RECEIVED",
        actor_account_id=actor_account_id,
        operation="result",
        key=work_item_id,
        payload={"work_item_id": work_item_id, "result_ref": ref, "status": payload["status"]},
    )
    if payload.get("display_identity") is not None:
        append_audit(
            session,
            action="work.display_identity_ignored",
            target_type="work_item",
            target_id=work_item_id,
            result="IGNORED",
            actor_label=agent_id,
            correlation_id=item.correlation_id,
            workspace_id=uuid.UUID(item.workspace_id),
            actor_account_id=uuid.UUID(actor_account_id),
            metadata={"reason": "development plan §7A.4: display identity is set by the server"},
            clock=clock,
        )
    return ResultOutcome(
        "RESULT_ACCEPTED",
        work_item_id,
        receipt.receipt_id,
        ref,
        res.event_id,
        load(session, work_item_id),
    )


def _duplicate(
    session: Session,
    item: WorkItem,
    existing: receipts.Receipt | None,
    ref: str,
    digest: str,
    actor_account_id: str,
    agent_id: str,
) -> ResultOutcome:
    first_ref = existing.result_ref if existing and existing.result_ref else ""
    dup = receipts.record_receipt(
        session,
        item.work_item_id,
        "duplicate_result",
        delivery_no=item.delivery_count,
        result_ref=ref,
        result_sha256=digest,
        detail={
            "first_result_ref": first_ref,
            "same_body": bool(existing and existing.result_sha256 == digest),
        },
    )
    append_audit(
        session,
        action="work.duplicate_result_ignored",
        target_type="work_item",
        target_id=item.work_item_id,
        result="IGNORED",
        error_code="DUPLICATE_RESULT_IGNORED",
        actor_label=agent_id,
        correlation_id=item.correlation_id,
        workspace_id=uuid.UUID(item.workspace_id),
        actor_account_id=uuid.UUID(actor_account_id),
        metadata={"first_result_ref": first_ref, "ignored_result_ref": ref},
    )
    return ResultOutcome(
        "DUPLICATE_RESULT_IGNORED",
        item.work_item_id,
        existing.receipt_id if existing else dup.receipt_id,
        first_ref or ref,
        None,
        load(session, item.work_item_id),
    )


def open_items(session: Session, *, agent_id: str | None = None) -> list[WorkItem]:
    where = "WHERE status IN ('QUEUED','DELIVERED','ACKED','IN_PROGRESS')"
    params: dict[str, Any] = {}
    if agent_id is not None:
        where += " AND agent_id = :a"
        params["a"] = agent_id
    rows = (
        session.execute(
            text(f"SELECT {_COLUMNS} FROM work_items {where} ORDER BY created_at, work_item_id"),  # noqa: S608
            params,
        )
        .mappings()
        .all()
    )
    return [_row_to_item(r) for r in rows]
