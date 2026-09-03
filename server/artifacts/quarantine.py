"""Artifact quarantine ledger and scan provenance (P6-03; validation plan V-P6-06).

A quarantined artifact is unreadable through the normal artifact path until an administrator
releases it. Every quarantine decision writes a redacted audit entry: the reason code and the
signature name, never the file bytes, the file name's original spelling or the caller's payload.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.artifacts.scan import ScanReport
from server.domain.clock import Clock
from server.observability.audit import append_audit

QUARANTINE_REASONS: tuple[str, ...] = (
    "ARTIFACT_MALWARE",
    "ARTIFACT_SCAN_UNAVAILABLE",
    "ARTIFACT_CHECKSUM_MISMATCH",
    "ARTIFACT_MISSING",
    "ARTIFACT_MIME_MISMATCH",
)


@dataclass(frozen=True)
class QuarantineRecord:
    artifact_id: str
    reason_code: str
    detail: str | None
    released_at: str | None

    @property
    def open(self) -> bool:
        return self.released_at is None


def record_scan(session: Session, artifact_id: str, report: ScanReport, clock: Clock) -> None:
    """Append scan provenance; the detail holds a signature name, never file content."""
    session.execute(
        text(
            "INSERT INTO artifact_scan_results (artifact_id, scanner, verdict, reason_code, "
            "detail, scanned_at) VALUES (:a, :s, :v, :r, :d, :at)"
        ),
        {
            "a": artifact_id,
            "s": report.scanner,
            "v": report.verdict,
            "r": report.reason_code,
            "d": (report.detail or "")[:200] or None,
            "at": clock.now(),
        },
    )


def quarantine(
    session: Session,
    *,
    workspace_id: str,
    artifact_id: str,
    reason_code: str,
    detail: str | None,
    actor_account_uuid: str | None,
    actor_label: str,
    correlation_id: str,
    clock: Clock,
) -> QuarantineRecord:
    """Mark the artifact quarantined, ledger the reason and write one redacted audit entry."""
    now = clock.now()
    session.execute(
        text("UPDATE artifacts SET status = 'quarantined' WHERE artifact_id = :a"),
        {"a": artifact_id},
    )
    session.execute(
        text(
            "INSERT INTO artifact_quarantine (artifact_id, workspace_id, reason_code, detail, "
            "scanned_at) VALUES (:a, :w, :r, :d, :at) ON CONFLICT (artifact_id) DO UPDATE SET "
            "reason_code = EXCLUDED.reason_code, detail = EXCLUDED.detail, "
            "scanned_at = EXCLUDED.scanned_at, released_by = NULL, released_at = NULL, "
            "release_reason = NULL"
        ),
        {
            "a": artifact_id,
            "w": uuid.UUID(workspace_id),
            "r": reason_code,
            "d": (detail or "")[:200] or None,
            "at": now,
        },
    )
    append_audit(
        session,
        action="artifact.quarantined",
        target_type="artifact",
        target_id=artifact_id,
        result="DENY",
        actor_label=actor_label,
        correlation_id=correlation_id,
        workspace_id=uuid.UUID(workspace_id),
        actor_account_id=None if actor_account_uuid is None else uuid.UUID(actor_account_uuid),
        error_code=reason_code,
        metadata={"reason_code": reason_code, "signature": (detail or "")[:120] or None},
        clock=clock,
    )
    return QuarantineRecord(artifact_id, reason_code, detail, None)


def release(
    session: Session,
    *,
    workspace_id: str,
    artifact_id: str,
    released_by: str,
    actor_label: str,
    reason: str,
    correlation_id: str,
    clock: Clock,
) -> bool:
    """Release a quarantined artifact back to ``registered``; audited. False when not held."""
    now = clock.now()
    updated = session.execute(
        text(
            "UPDATE artifact_quarantine SET released_by = :b, released_at = :at, "
            "release_reason = :reason WHERE artifact_id = :a AND workspace_id = :w "
            "AND released_at IS NULL"
        ),
        {
            "a": artifact_id,
            "w": uuid.UUID(workspace_id),
            "b": uuid.UUID(released_by),
            "at": now,
            "reason": reason[:200],
        },
    )
    if not (updated.rowcount or 0):  # type: ignore[attr-defined]
        return False
    session.execute(
        text("UPDATE artifacts SET status = 'registered' WHERE artifact_id = :a"),
        {"a": artifact_id},
    )
    append_audit(
        session,
        action="artifact.quarantine_released",
        target_type="artifact",
        target_id=artifact_id,
        result="OK",
        actor_label=actor_label,
        correlation_id=correlation_id,
        workspace_id=uuid.UUID(workspace_id),
        actor_account_id=uuid.UUID(released_by),
        metadata={"reason": reason[:120]},
        clock=clock,
    )
    return True


def status_of(session: Session, artifact_id: str) -> QuarantineRecord | None:
    row = session.execute(
        text(
            "SELECT artifact_id, reason_code, detail, released_at FROM artifact_quarantine "
            "WHERE artifact_id = :a"
        ),
        {"a": artifact_id},
    ).first()
    if row is None:
        return None
    return QuarantineRecord(
        str(row[0]), str(row[1]), row[2], None if row[3] is None else row[3].isoformat()
    )


def scans_of(session: Session, artifact_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            "SELECT scanner, verdict, reason_code, detail, scanned_at FROM artifact_scan_results "
            "WHERE artifact_id = :a ORDER BY scanned_at DESC, id DESC"
        ),
        {"a": artifact_id},
    ).all()
    return [
        {
            "scanner": r[0],
            "verdict": r[1],
            "reason_code": r[2],
            "detail": r[3],
            "scanned_at": r[4].isoformat(),
        }
        for r in rows
    ]
