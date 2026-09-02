"""AuditEvent appends (spec §9.1, §15.20). Values are never recorded, only redacted metadata."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.clock import Clock, SystemClock
from server.events.chain import AUDIT_CHAIN, chain_hash, hashed_row_fields, last_hash

_REDACT_KEYS = {"token", "password", "secret", "authorization", "cookie", "dsn", "key", "value"}


def redact_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (metadata or {}).items():
        if any(s in k.lower() for s in _REDACT_KEYS):
            out[k] = "<redacted>"
        elif isinstance(v, dict):
            out[k] = redact_metadata(v)
        else:
            out[k] = v
    return out


def append_audit(
    session: Session,
    *,
    action: str,
    target_type: str,
    target_id: str,
    result: str,
    actor_label: str,
    correlation_id: str,
    workspace_id: uuid.UUID | None = None,
    actor_account_id: uuid.UUID | None = None,
    error_code: str | None = None,
    metadata: dict[str, Any] | None = None,
    clock: Clock | None = None,
) -> str:
    """Append one audit row to the hash chain inside the caller's transaction; returns audit_id."""
    now = (clock or SystemClock()).now()
    # serialize appends on the audit chain so previous_hash is always the true predecessor
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext('audit_chain'))"))
    previous = last_hash(session, AUDIT_CHAIN)
    audit_id = "aud-" + uuid.uuid4().hex[:20]
    fields = {
        "audit_id": audit_id,
        "workspace_id": workspace_id,
        "actor_account_id": actor_account_id,
        "actor_label": actor_label,
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "result": result,
        "error_code": error_code,
        "correlation_id": correlation_id,
        "redacted_metadata": redact_metadata(metadata),
        "occurred_at": now,
    }
    content_hash = chain_hash(hashed_row_fields(AUDIT_CHAIN, fields), previous)
    session.execute(
        text(
            "INSERT INTO audit_events (audit_id, workspace_id, actor_account_id, actor_label, "
            "action, "
            "target_type, target_id, result, error_code, correlation_id, redacted_metadata, "
            "previous_hash, content_hash, occurred_at) VALUES (:audit_id, :workspace_id, "
            ":actor_account_id, :actor_label, :action, :target_type, :target_id, :result, "
            ":error_code, :correlation_id, CAST(:redacted_metadata AS jsonb), :previous_hash, "
            ":content_hash, :occurred_at)"
        ),
        {
            **fields,
            "redacted_metadata": __import__("json").dumps(fields["redacted_metadata"]),
            "previous_hash": previous,
            "content_hash": content_hash,
        },
    )
    return audit_id


def utc_today(clock: Clock | None = None) -> dt.date:
    return (clock or SystemClock()).now().date()
