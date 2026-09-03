"""Signed, append-only key-tombstone ledger (development plan §9.3; P4-05/P4-11).

The Phase 1 ``key_tombstones`` chain records every destroyed DEK. This module signs each entry
with a ledger key that is separate from the master key, the database and its backups
(``AGENT_COLAB_LEDGER_KEY_B64``), verifies the chain and signatures, and reconciles a restored
database against the ledger: any DEK named by a tombstone is (re)destroyed and can never be
registered as a decryption target again (V-P4-25/V-P4-29).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.events.chain import TOMBSTONE_CHAIN, chain_hash, hashed_row_fields, last_hash
from server.secrets.provider import SecretError

ENV_LEDGER_KEY = "AGENT_COLAB_LEDGER_KEY_B64"  # nosec B105 - environment variable name
ENV_LEDGER_KEY_ID = "AGENT_COLAB_LEDGER_KEY_ID"  # nosec B105 - environment variable name


@dataclass(frozen=True)
class LedgerKey:
    key_id: str
    key: bytes

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LedgerKey:
        env = os.environ if env is None else env
        value = env.get(ENV_LEDGER_KEY)
        if not value:
            raise SecretError("SECRET_PROVIDER_UNAVAILABLE", "no ledger key configured")
        raw = base64.b64decode(value)
        if len(raw) < 32:
            raise SecretError("SECRET_PROVIDER_UNAVAILABLE", "ledger key too short")
        return cls(env.get(ENV_LEDGER_KEY_ID, "lk-local-1"), raw)


def new_ledger_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def sign_entry(key: LedgerKey, content_hash: str) -> str:
    return hmac.new(key.key, content_hash.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Tombstone:
    key_ref: str
    workspace_id: uuid.UUID
    target_type: str
    target_id: str
    reason: str
    content_hash: str
    signature: str | None
    destroyed_at: dt.datetime


def record_tombstone(
    session: Session,
    key: LedgerKey,
    *,
    dek_id: str,
    workspace_id: uuid.UUID,
    target_type: str,
    target_id: str,
    reason: str,
    requested_by: uuid.UUID,
    now: dt.datetime,
    audit_event_id: str | None = None,
) -> Tombstone:
    """Append one signed entry (the DEK itself must already be shredded by the caller)."""
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext('key_tombstones_chain'))"))
    if session.execute(
        text("SELECT 1 FROM key_tombstones WHERE key_ref = :k"), {"k": dek_id}
    ).first():
        raise SecretError("SECRET_HANDLE_REVOKED", f"{dek_id} already tombstoned")
    previous = last_hash(session, TOMBSTONE_CHAIN)
    fields = {
        "key_ref": dek_id,
        "workspace_id": workspace_id,
        "target_type": target_type,
        "target_id": target_id,
        "reason": reason,
        "requested_by": requested_by,
        "audit_event_id": audit_event_id,
        "destroyed_at": now,
    }
    content_hash = chain_hash(hashed_row_fields(TOMBSTONE_CHAIN, fields), previous)
    signature = sign_entry(key, content_hash)
    session.execute(
        text(
            "INSERT INTO key_tombstones (key_ref, workspace_id, target_type, target_id, reason, "
            "requested_by, audit_event_id, previous_hash, content_hash, destroyed_at, signature, "
            "ledger_key_id) VALUES (:key_ref, :workspace_id, :target_type, :target_id, :reason, "
            ":requested_by, :audit_event_id, :prev, :hash, :destroyed_at, :sig, :kid)"
        ),
        {**fields, "prev": previous, "hash": content_hash, "sig": signature, "kid": key.key_id},
    )
    return Tombstone(
        dek_id, workspace_id, target_type, target_id, reason, content_hash, signature, now
    )


def is_destroyed(session: Session, dek_id: str) -> bool:
    return (
        session.execute(
            text("SELECT 1 FROM key_tombstones WHERE key_ref = :k"), {"k": dek_id}
        ).first()
        is not None
    )


def entries(session: Session) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in session.execute(
            text(
                "SELECT key_ref, workspace_id, target_type, target_id, reason, requested_by, "
                "audit_event_id, previous_hash, content_hash, destroyed_at, signature, "
                "ledger_key_id FROM key_tombstones ORDER BY id"
            )
        ).mappings()
    ]


def verify_chain(session: Session, key: LedgerKey | None = None) -> list[str]:
    """Problems in the tombstone chain: broken links, recomputed-hash mismatches, and — when a
    ledger key is given — missing or invalid signatures on entries made by that key."""
    problems: list[str] = []
    previous: str | None = None
    for row in entries(session):
        fields = {
            k: row[k]
            for k in (
                "key_ref",
                "workspace_id",
                "target_type",
                "target_id",
                "reason",
                "requested_by",
                "audit_event_id",
                "destroyed_at",
            )
        }
        expected = chain_hash(hashed_row_fields(TOMBSTONE_CHAIN, fields), previous)
        if row["previous_hash"] != previous:
            problems.append(f"{row['key_ref']}: broken link")
        if row["content_hash"] != expected:
            problems.append(f"{row['key_ref']}: hash mismatch")
        if key is not None and row["ledger_key_id"] == key.key_id:
            if not row["signature"] or not hmac.compare_digest(
                row["signature"], sign_entry(key, str(row["content_hash"]))
            ):
                problems.append(f"{row['key_ref']}: signature invalid")
        previous = str(row["content_hash"])
    return problems


@dataclass(frozen=True)
class ReconcileReport:
    tombstones: int
    secret_versions_destroyed: int
    sensitive_keys_destroyed: int
    problems: tuple[str, ...]


def reconcile_tombstones(
    session: Session, key: LedgerKey | None = None, *, now: dt.datetime | None = None
) -> ReconcileReport:
    """Restore-time reconciliation: every tombstoned DEK is shredded again (a backup taken before
    the deletion still carries the wrapped DEK) and marked destroyed so it is never a decryption
    target. Runs before the service opens; idempotent."""
    when = now or dt.datetime.now(dt.UTC)
    problems = verify_chain(session, key)
    versions = 0
    keys = 0
    for row in entries(session):
        dek_id = str(row["key_ref"])
        res = session.execute(
            text(
                "UPDATE secret_versions SET wrapped_dek = NULL, status = 'destroyed', "
                "destroyed_at = COALESCE(destroyed_at, :t) WHERE dek_id = :d "
                "AND status <> 'destroyed'"
            ),
            {"d": dek_id, "t": when},
        )
        versions += int(res.rowcount or 0)  # type: ignore[attr-defined]
        res2 = session.execute(
            text(
                "UPDATE sensitive_keys SET wrapped_dek = NULL, status = 'destroyed', "
                "destroyed_at = COALESCE(destroyed_at, :t) WHERE key_ref = :d "
                "AND status <> 'destroyed'"
            ),
            {"d": dek_id, "t": when},
        )
        keys += int(res2.rowcount or 0)  # type: ignore[attr-defined]
    return ReconcileReport(len(entries(session)), versions, keys, tuple(problems))
