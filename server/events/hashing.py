"""Event content hashing and per-aggregate hash chain (development plan §6.3, spec §9.3).

``content_hash = SHA-256(canonical_json(hash_body))`` where ``hash_body`` is the immutable
envelope metadata, the non-sensitive payload, the SHA-256 of the sensitive ciphertext (or null),
and ``previous_hash`` (the content hash of the previous Event of the same aggregate, null for
``aggregate_seq == 1``). ``recorded_at`` and ``content_hash`` itself are excluded. The full
definition is in ``docs/protocol/event-contract.md``.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from server.events.canonical import canonical_json

HASHED_METADATA_FIELDS = (
    "event_id",
    "schema_version",
    "workspace_id",
    "aggregate_type",
    "aggregate_id",
    "aggregate_seq",
    "channel_id",
    "task_id",
    "type",
    "actor_account_id",
    "caused_by",
    "correlation_id",
    "idempotency_scope",
    "idempotency_key",
    "policy_version",
    "occurred_at",
    "sensitive_payload_key_ref",
)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ciphertext_digest(ciphertext_b64: str | None) -> str | None:
    if ciphertext_b64 is None:
        return None
    return sha256_hex(base64.b64decode(ciphertext_b64, validate=True))


def hash_body(event: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {k: event.get(k) for k in HASHED_METADATA_FIELDS}
    body["payload"] = event.get("payload", {})
    body["sensitive_payload_sha256"] = ciphertext_digest(event.get("sensitive_payload_ciphertext"))
    body["previous_hash"] = event.get("previous_hash")
    return body


def compute_content_hash(event: dict[str, Any]) -> str:
    return sha256_hex(canonical_json(hash_body(event)))


def verify_content_hash(event: dict[str, Any]) -> bool:
    return event.get("content_hash") == compute_content_hash(event)


def verify_chain(events: list[dict[str, Any]]) -> list[str]:
    """Verify a single aggregate's stream ordered by ``aggregate_seq``; return problems."""
    problems: list[str] = []
    previous: str | None = None
    for expected_seq, ev in enumerate(events, start=1):
        eid = ev.get("event_id", "?")
        if ev.get("aggregate_seq") != expected_seq:
            problems.append(f"{eid}: aggregate_seq {ev.get('aggregate_seq')} != {expected_seq}")
        if ev.get("previous_hash") != previous:
            problems.append(f"{eid}: previous_hash does not match the preceding content_hash")
        if not verify_content_hash(ev):
            problems.append(f"{eid}: content_hash mismatch")
        previous = ev.get("content_hash")
    return problems
