"""Event store interface (P1-02 implements ``PostgresEventStore``; other packages code against
``EventStore``). ``InMemoryEventStore`` supports unit tests without a database.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from server.domain.clock import Clock, SystemClock, isoformat_utc
from server.events.canonical import canonical_json
from server.events.hashing import compute_content_hash


class EventStoreError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class AppendRequest:
    """One Event to append. ``expected_seq`` enables optimistic concurrency (None = next)."""

    workspace_id: str
    aggregate_type: str
    aggregate_id: str
    type: str
    actor_account_id: str
    correlation_id: str
    idempotency_scope: str
    idempotency_key: str
    payload: dict[str, Any]
    policy_version: str = "policy-v1"
    channel_id: str | None = None
    task_id: str | None = None
    caused_by: str | None = None
    expected_seq: int | None = None
    sensitive: dict[str, Any] | None = None  # encrypted by the store, never stored in payload
    schema_version: int = 1

    def request_body_hash(self) -> str:
        body = {
            "workspace_id": self.workspace_id,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "type": self.type,
            "payload": self.payload,
            "sensitive": self.sensitive,
            "channel_id": self.channel_id,
            "task_id": self.task_id,
            "caused_by": self.caused_by,
        }
        return hashlib.sha256(canonical_json(body)).hexdigest()


@dataclass(frozen=True)
class AppendResult:
    event_id: str
    aggregate_seq: int
    content_hash: str
    recorded_seq: int
    replayed: bool = False  # True when an identical idempotent request returned the original


class EventStore(Protocol):
    def append(self, request: AppendRequest) -> AppendResult: ...

    def stream(
        self, workspace_id: str, aggregate_type: str, aggregate_id: str
    ) -> list[dict[str, Any]]: ...


@dataclass
class InMemoryEventStore:
    """Deterministic in-memory store with the same idempotency/sequence semantics (tests only)."""

    clock: Clock = field(default_factory=SystemClock)
    events: list[dict[str, Any]] = field(default_factory=list)
    _idem: dict[tuple[str, str, str, str], tuple[str, str]] = field(default_factory=dict)

    def _last(self, ws: str, agg_type: str, agg_id: str) -> dict[str, Any] | None:
        for ev in reversed(self.events):
            if (ev["workspace_id"], ev["aggregate_type"], ev["aggregate_id"]) == (
                ws,
                agg_type,
                agg_id,
            ):
                return ev
        return None

    def append(self, request: AppendRequest) -> AppendResult:
        key = (
            request.workspace_id,
            request.actor_account_id,
            request.idempotency_scope,
            request.idempotency_key,
        )
        body_hash = request.request_body_hash()
        if key in self._idem:
            prior_hash, prior_event_id = self._idem[key]
            if prior_hash != body_hash:
                raise EventStoreError(
                    "IDEMPOTENCY_CONFLICT", "same idempotency key with a different body"
                )
            prior = next(e for e in self.events if e["event_id"] == prior_event_id)
            return AppendResult(
                prior["event_id"],
                prior["aggregate_seq"],
                prior["content_hash"],
                prior["recorded_seq"],
                True,
            )
        last = self._last(request.workspace_id, request.aggregate_type, request.aggregate_id)
        next_seq = (last["aggregate_seq"] + 1) if last else 1
        if request.expected_seq is not None and request.expected_seq != next_seq:
            raise EventStoreError(
                "SEQUENCE_CONFLICT", f"expected {request.expected_seq}, next is {next_seq}"
            )
        if request.caused_by is not None and not any(
            e["event_id"] == request.caused_by for e in self.events
        ):
            raise EventStoreError("CAUSED_BY_UNKNOWN", request.caused_by)
        event: dict[str, Any] = {
            "event_id": "evt-" + uuid.uuid4().hex,
            "schema_version": request.schema_version,
            "workspace_id": request.workspace_id,
            "aggregate_type": request.aggregate_type,
            "aggregate_id": request.aggregate_id,
            "aggregate_seq": next_seq,
            "channel_id": request.channel_id,
            "task_id": request.task_id,
            "type": request.type,
            "actor_account_id": request.actor_account_id,
            "caused_by": request.caused_by,
            "correlation_id": request.correlation_id,
            "idempotency_scope": request.idempotency_scope,
            "idempotency_key": request.idempotency_key,
            "policy_version": request.policy_version,
            "payload": request.payload,
            "sensitive_payload_ciphertext": None,
            "sensitive_payload_key_ref": None,
            "previous_hash": last["content_hash"] if last else None,
            "occurred_at": isoformat_utc(self.clock.now()),
        }
        event["content_hash"] = compute_content_hash(event)
        event["recorded_seq"] = len(self.events) + 1
        self.events.append(event)
        self._idem[key] = (body_hash, event["event_id"])
        return AppendResult(
            event["event_id"], next_seq, event["content_hash"], event["recorded_seq"]
        )

    def stream(
        self, workspace_id: str, aggregate_type: str, aggregate_id: str
    ) -> list[dict[str, Any]]:
        return [
            e
            for e in self.events
            if (e["workspace_id"], e["aggregate_type"], e["aggregate_id"])
            == (workspace_id, aggregate_type, aggregate_id)
        ]
