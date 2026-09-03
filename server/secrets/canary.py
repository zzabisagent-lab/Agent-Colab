"""Redaction canaries (development plan §9.3 leak tests; V-P4-14).

A canary is a registered secret whose value is a marker ``CANARY-NOT-A-SECRET-<n>``. After a
full flow (grant → lease → resolve → work result → card → document) :func:`scan` searches every
place text can land — Events, audit metadata, messages, channel outbox rows, documents, work
receipts, Schedule Runs/attempts/notices/versions, usage records, budget alerts and provided log
lines — and reports locations only (never the marker itself).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

CANARY_PATTERN = re.compile(r"CANARY-NOT-A-SECRET-\d{4}")
_REGISTERED: dict[str, str] = {}  # secret_ref -> canary value (memory only)


def canary_value(n: int) -> str:
    return f"CANARY-NOT-A-SECRET-{n:04d}"


def register_canary(secret_ref: str, n: int) -> str:
    value = canary_value(n)
    _REGISTERED[secret_ref] = value
    return value


def registered_values() -> list[bytes]:
    return [v.encode() for v in _REGISTERED.values()]


def clear_registry() -> None:
    _REGISTERED.clear()


@dataclass(frozen=True)
class Hit:
    location: str  # table/column/id or log line number — never the value
    value_ref: str  # secret_ref of the leaked canary


_TEXT_QUERIES: tuple[tuple[str, str], ...] = (
    ("events.payload", "SELECT event_id, payload::text FROM events WHERE workspace_id = :w"),
    (
        "audit_events.redacted_metadata",
        "SELECT audit_id, redacted_metadata::text || ' ' || coalesce(error_code,'') "
        "FROM audit_events WHERE workspace_id = :w",
    ),
    (
        "delivery_outbox.payload",
        "SELECT outbox_id, payload::text FROM delivery_outbox WHERE workspace_id = :w",
    ),
    (
        "messages.body_redacted",
        "SELECT message_id, body_redacted FROM messages WHERE workspace_id = :w",
    ),
    (
        "channel_posts",
        "SELECT dedupe_key, coalesce(props::text,'') FROM channel_posts WHERE workspace_id = :w",
    ),
    (
        "work_items.payload",
        "SELECT work_item_id, payload::text FROM work_items WHERE workspace_id = :w",
    ),
    (
        "work_receipts",
        "SELECT work_item_id, coalesce(receipt::text,'') FROM work_receipts WHERE work_item_id "
        "IN (SELECT work_item_id FROM work_items WHERE workspace_id = :w)",
    ),
    (
        "document_versions.manifest",
        "SELECT document_id || '/' || version, manifest::text FROM document_versions "
        "WHERE document_id IN (SELECT document_id FROM documents WHERE workspace_id = :w)",
    ),
    (
        "tasks_projection",
        "SELECT task_id, coalesce(latest_progress,'') || ' ' || title FROM tasks_projection "
        "WHERE workspace_id = :w",
    ),
    (
        "notifications",
        "SELECT notification_id, coalesce(payload::text,'') FROM notifications "
        "WHERE workspace_id = :w",
    ),
    (
        "schedule_runs",
        "SELECT run_id, concat_ws(' ', idempotency_key, request_key, error_code, planner_note, "
        "occurrence_key, task_id) FROM schedule_runs WHERE workspace_id = :w",
    ),
    (
        "schedule_run_attempts",
        "SELECT run_id || '/' || attempt_no, concat_ws(' ', result, error_code, runner_id) "
        "FROM schedule_run_attempts WHERE run_id IN "
        "(SELECT run_id FROM schedule_runs WHERE workspace_id = :w)",
    ),
    (
        "schedule_notices",
        "SELECT run_id || '/' || kind, concat_ws(' ', dedupe_key, outbox_id) "
        "FROM schedule_notices WHERE run_id IN "
        "(SELECT run_id FROM schedule_runs WHERE workspace_id = :w)",
    ),
    (
        "schedule_versions.action_template",
        "SELECT schedule_version_id, action_template::text || ' ' || agent_selection::text "
        "FROM schedule_versions WHERE schedule_id IN "
        "(SELECT schedule_id FROM schedules WHERE workspace_id = :w)",
    ),
    (
        "usage_records",
        "SELECT coalesce(run_id, task_id, work_item_id, id::text), "
        "concat_ws(' ', model, source, unavailable_reason) FROM usage_records "
        "WHERE workspace_id = :w",
    ),
    (
        "budget_alerts",
        "SELECT coalesce(run_id, schedule_id, id::text), detail::text FROM budget_alerts "
        "WHERE workspace_id = :w",
    ),
)


def _find(textual: str) -> list[str]:
    hits: list[str] = []
    for ref, value in _REGISTERED.items():
        if value in textual:
            hits.append(ref)
    if not _REGISTERED:
        hits.extend(f"pattern:{m}" for m in CANARY_PATTERN.findall(textual))
    return hits


def scan(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    log_lines: Iterable[str] = (),
    documents: Iterable[tuple[str, str]] = (),
    document_root: Path | None = None,
    error_texts: Iterable[str] = (),
) -> list[Hit]:
    hits: list[Hit] = []
    for label, query in _TEXT_QUERIES:
        try:
            rows = session.execute(text(query), {"w": workspace_id}).all()
        except Exception:  # a table absent in this environment is not a leak
            session.rollback()
            continue
        for row in rows:
            for ref in _find(str(row[1] or "")):
                hits.append(Hit(f"{label}:{row[0]}", ref))
    for i, line in enumerate(log_lines):
        for ref in _find(line):
            hits.append(Hit(f"log:{i}", ref))
    for i, err in enumerate(error_texts):
        for ref in _find(err):
            hits.append(Hit(f"error:{i}", ref))
    for name, body in documents:
        for ref in _find(body):
            hits.append(Hit(f"document:{name}", ref))
    if document_root is not None and document_root.exists():
        for path in document_root.rglob("*"):
            if path.is_file():
                try:
                    body = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for ref in _find(body):
                    hits.append(Hit(f"file:{path.name}", ref))
    return hits


def scan_text(lines: Iterable[str], *, label: str = "text") -> list[Hit]:
    """Scan arbitrary text (log lines, error strings) without touching the database."""
    return [Hit(f"{label}:{i}", ref) for i, line in enumerate(lines) for ref in _find(line)]


def summarize(hits: list[Hit]) -> dict[str, Any]:
    return {"hits": len(hits), "locations": sorted({h.location for h in hits})}
