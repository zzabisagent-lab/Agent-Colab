"""PostgreSQL Event store (P1-02; development plan §6.3, spec §9.3).

Append algorithm inside the caller's transaction:
1. contract validation (type/payload) via the schema registry;
2. per-aggregate advisory lock ``(workspace, aggregate_type, aggregate_id)``;
3. scoped idempotency: same ``(workspace, actor, scope, key)`` with the same body hash returns
   the original Event (``replayed``); a different body is ``IDEMPOTENCY_CONFLICT``;
4. causality: ``caused_by`` must exist in the same workspace;
5. workspace consistency of actor, channel, and task;
6. sequence: next ``aggregate_seq`` (or ``expected_seq`` compare-and-swap → ``SEQUENCE_CONFLICT``);
7. hash chain (``previous_hash`` = last content hash) and envelope encryption of sensitive data;
8. INSERT; a concurrent duplicate on the idempotency unique index is resolved by re-reading.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.domain.clock import Clock, SystemClock, isoformat_utc
from server.events.contract import ContractError, SchemaRegistry, default_registry
from server.events.hashing import compute_content_hash
from server.events.store import AppendRequest, AppendResult, EventStoreError
from server.secrets.envelope import CryptoError, EnvelopeCrypto

_INSERT = text(
    "INSERT INTO events (id, event_id, schema_version, workspace_id, aggregate_type, aggregate_id, "
    "aggregate_seq, channel_id, task_id, type, actor_account_id, caused_by, correlation_id, "
    "idempotency_scope, idempotency_key, request_body_hash, policy_version, payload, "
    "sensitive_payload_ciphertext, sensitive_payload_key_ref, previous_hash, content_hash, "
    "occurred_at) "
    "VALUES (:id, :event_id, :schema_version, :workspace_id, :aggregate_type, :aggregate_id, "
    ":aggregate_seq, :channel_id, :task_id, :type, :actor_account_id, :caused_by, :correlation_id, "
    ":idempotency_scope, :idempotency_key, :request_body_hash, :policy_version, CAST(:payload AS "
    "jsonb), "
    ":ciphertext, :key_ref, :previous_hash, :content_hash, :occurred_at) RETURNING recorded_seq"
)

_COLUMNS = (
    "event_id, schema_version, workspace_id, aggregate_type, aggregate_id, aggregate_seq, "
    "channel_id, "
    "task_id, type, actor_account_id, caused_by, correlation_id, idempotency_scope, "
    "idempotency_key, "
    "policy_version, payload, sensitive_payload_ciphertext, sensitive_payload_key_ref, "
    "previous_hash, "
    "content_hash, occurred_at, recorded_at, recorded_seq, request_body_hash"
)


def row_to_event(row: Any) -> dict[str, Any]:
    ev = dict(row)
    for k in ("workspace_id", "channel_id", "actor_account_id"):
        if ev.get(k) is not None:
            ev[k] = str(ev[k])
    ct = ev.get("sensitive_payload_ciphertext")
    ev["sensitive_payload_ciphertext"] = (
        base64.b64encode(bytes(ct)).decode() if ct is not None else None
    )
    ev["occurred_at"] = isoformat_utc(ev["occurred_at"])
    ev["recorded_at"] = isoformat_utc(ev["recorded_at"])
    return ev


class PostgresEventStore:
    def __init__(
        self,
        session: Session,
        crypto: EnvelopeCrypto | None = None,
        clock: Clock | None = None,
        registry: SchemaRegistry | None = None,
    ) -> None:
        self._s = session
        self._crypto = crypto
        self._clock = clock or SystemClock()
        self._registry = registry or default_registry()

    # -- helpers ---------------------------------------------------------------------------
    def _lock(self, ws: str, agg_type: str, agg_id: str) -> None:
        self._s.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"{ws}|{agg_type}|{agg_id}"}
        )

    def _find_idempotent(self, r: AppendRequest) -> dict[str, Any] | None:
        row = (
            self._s.execute(
                text(
                    f"SELECT {_COLUMNS} FROM events WHERE workspace_id = :ws AND actor_account_id "  # noqa: S608
                    f"= :actor "
                    "AND idempotency_scope = :scope AND idempotency_key = :key"
                ),
                {
                    "ws": uuid.UUID(r.workspace_id),
                    "actor": uuid.UUID(r.actor_account_id),
                    "scope": r.idempotency_scope,
                    "key": r.idempotency_key,
                },
            )
            .mappings()
            .first()
        )
        return row_to_event(row) if row else None

    def _replay(self, prior: dict[str, Any], body_hash: str) -> AppendResult:
        if prior["request_body_hash"] != body_hash:
            raise EventStoreError(
                "IDEMPOTENCY_CONFLICT", "same idempotency key with a different body"
            )
        return AppendResult(
            prior["event_id"],
            prior["aggregate_seq"],
            prior["content_hash"],
            prior["recorded_seq"],
            True,
        )

    def _check_workspace(self, r: AppendRequest) -> None:
        ws = uuid.UUID(r.workspace_id)
        actor_ws = self._s.execute(
            text("SELECT workspace_id FROM accounts WHERE id = :a"),
            {"a": uuid.UUID(r.actor_account_id)},
        ).scalar()
        if actor_ws is None:
            raise EventStoreError("ACTOR_UNKNOWN", r.actor_account_id)
        if actor_ws != ws:
            raise EventStoreError("WORKSPACE_MISMATCH", "actor belongs to another workspace")
        if r.channel_id is not None:
            ch_ws = self._s.execute(
                text("SELECT workspace_id FROM channels WHERE id = :c"),
                {"c": uuid.UUID(r.channel_id)},
            ).scalar()
            if ch_ws is None:
                raise EventStoreError("CHANNEL_UNKNOWN", r.channel_id)
            if ch_ws != ws:
                raise EventStoreError("WORKSPACE_MISMATCH", "channel belongs to another workspace")
        if r.task_id is not None:
            task_ws = self._s.execute(
                text("SELECT workspace_id FROM tasks_projection WHERE task_id = :t"),
                {"t": r.task_id},
            ).scalar()
            if task_ws is not None and task_ws != ws:
                raise EventStoreError("WORKSPACE_MISMATCH", "task belongs to another workspace")
        if r.caused_by is not None:
            cause = self._s.execute(
                text("SELECT workspace_id FROM events WHERE event_id = :e"), {"e": r.caused_by}
            ).scalar()
            if cause is None:
                raise EventStoreError("CAUSED_BY_UNKNOWN", r.caused_by)
            if cause != ws:
                raise EventStoreError(
                    "WORKSPACE_MISMATCH", "caused_by belongs to another workspace"
                )

    def _validate_contract(self, event: dict[str, Any]) -> None:
        try:
            self._registry.validate_envelope(event)
            self._registry.validate_type(event)
        except ContractError as exc:
            raise EventStoreError(exc.code, exc.detail) from exc

    # -- API -------------------------------------------------------------------------------
    def append(self, r: AppendRequest) -> AppendResult:
        body_hash = r.request_body_hash()
        self._lock(r.workspace_id, r.aggregate_type, r.aggregate_id)
        prior = self._find_idempotent(r)
        if prior is not None:
            return self._replay(prior, body_hash)
        self._check_workspace(r)
        last = self._s.execute(
            text(
                "SELECT aggregate_seq, content_hash FROM events WHERE workspace_id = :ws AND "
                "aggregate_type = :t "
                "AND aggregate_id = :a ORDER BY aggregate_seq DESC LIMIT 1"
            ),
            {"ws": uuid.UUID(r.workspace_id), "t": r.aggregate_type, "a": r.aggregate_id},
        ).first()
        next_seq = int(last[0]) + 1 if last else 1
        if r.expected_seq is not None and r.expected_seq != next_seq:
            raise EventStoreError(
                "SEQUENCE_CONFLICT", f"expected {r.expected_seq}, next is {next_seq}"
            )
        ciphertext: bytes | None = None
        key_ref: str | None = None
        if r.sensitive is not None:
            if self._crypto is None:
                raise EventStoreError("SENSITIVE_UNSUPPORTED", "no envelope crypto configured")
            try:
                ciphertext, key_ref = self._crypto.encrypt(
                    self._s, r.workspace_id, r.aggregate_type, r.aggregate_id, r.sensitive
                )
            except CryptoError as exc:
                raise EventStoreError(exc.code, exc.detail) from exc
        event: dict[str, Any] = {
            "event_id": "evt-" + uuid.uuid4().hex,
            "schema_version": r.schema_version,
            "workspace_id": r.workspace_id,
            "aggregate_type": r.aggregate_type,
            "aggregate_id": r.aggregate_id,
            "aggregate_seq": next_seq,
            "channel_id": r.channel_id,
            "task_id": r.task_id,
            "type": r.type,
            "actor_account_id": r.actor_account_id,
            "caused_by": r.caused_by,
            "correlation_id": r.correlation_id,
            "idempotency_scope": r.idempotency_scope,
            "idempotency_key": r.idempotency_key,
            "policy_version": r.policy_version,
            "payload": r.payload,
            "sensitive_payload_ciphertext": base64.b64encode(ciphertext).decode()
            if ciphertext
            else None,
            "sensitive_payload_key_ref": key_ref,
            "previous_hash": str(last[1]) if last else None,
            "occurred_at": isoformat_utc(self._clock.now()),
        }
        event["content_hash"] = compute_content_hash(event)
        self._validate_contract(event)
        import json

        params = {
            **{
                k: v
                for k, v in event.items()
                if k
                not in (
                    "payload",
                    "sensitive_payload_ciphertext",
                    "workspace_id",
                    "channel_id",
                    "actor_account_id",
                    "occurred_at",
                )
            },
            "id": uuid.uuid4(),
            "workspace_id": uuid.UUID(r.workspace_id),
            "channel_id": uuid.UUID(r.channel_id) if r.channel_id else None,
            "actor_account_id": uuid.UUID(r.actor_account_id),
            "payload": json.dumps(r.payload),
            "ciphertext": ciphertext,
            "key_ref": key_ref,
            "request_body_hash": body_hash,
            "occurred_at": self._clock.now(),
        }
        try:
            with self._s.begin_nested():
                recorded_seq = self._s.execute(
                    _INSERT, {**params, "occurred_at": params["occurred_at"]}
                ).scalar_one()
        except IntegrityError as exc:
            constraint = getattr(exc.orig, "diag", None)
            name = getattr(constraint, "constraint_name", "") or str(exc.orig)
            if "idempotency" in name:
                prior = self._find_idempotent(r)
                if prior is not None:
                    return self._replay(prior, body_hash)
            if "aggregate_seq" in name:
                raise EventStoreError(
                    "SEQUENCE_CONFLICT", "concurrent append on the aggregate"
                ) from exc
            raise EventStoreError("EVENT_INSERT_FAILED", name) from exc
        return AppendResult(event["event_id"], next_seq, event["content_hash"], int(recorded_seq))

    def stream(
        self, workspace_id: str, aggregate_type: str, aggregate_id: str
    ) -> list[dict[str, Any]]:
        rows = self._s.execute(
            text(
                f"SELECT {_COLUMNS} FROM events WHERE workspace_id = :ws AND aggregate_type = :t "  # noqa: S608
                "AND aggregate_id = :a ORDER BY aggregate_seq"
            ),
            {"ws": uuid.UUID(workspace_id), "t": aggregate_type, "a": aggregate_id},
        ).mappings()
        return [row_to_event(r) for r in rows]

    def read_since(
        self, workspace_id: str, after_recorded_seq: int, limit: int = 100
    ) -> list[dict[str, Any]]:
        rows = self._s.execute(
            text(
                f"SELECT {_COLUMNS} FROM events WHERE workspace_id = :ws AND recorded_seq > :after "  # noqa: S608
                "ORDER BY recorded_seq LIMIT :lim"
            ),
            {
                "ws": uuid.UUID(workspace_id),
                "after": after_recorded_seq,
                "lim": min(max(limit, 1), 1000),
            },
        ).mappings()
        return [row_to_event(r) for r in rows]

    def get(self, event_id: str) -> dict[str, Any] | None:
        row = (
            self._s.execute(
                text(f"SELECT {_COLUMNS} FROM events WHERE event_id = :e"),  # noqa: S608
                {"e": event_id},
            )
            .mappings()
            .first()
        )
        return row_to_event(row) if row else None
