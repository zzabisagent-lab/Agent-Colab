"""Completion gate (spec §8.2, §21.1 Task closure; V-P1-14).

A Task (or any verified target) may complete only when the latest revision of its latest
VerificationRun is ``PASSED``. P1-10 adds the FINALIZED Document prerequisite through
``server.domain.task.register_completion_check``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.application.bus import CommandError


@dataclass(frozen=True)
class VerificationGate:
    passed: bool
    verification_id: str | None
    revision: int
    result: str | None


def verification_gate(session: Session, target_type: str, target_id: str) -> VerificationGate:
    row = session.execute(
        text(
            "SELECT verification_id, current_revision, result FROM verification_runs "
            "WHERE target_type = :t AND target_id = :i AND status <> 'CANCELLED' "
            "ORDER BY created_at DESC, id DESC LIMIT 1"
        ),
        {"t": target_type, "i": target_id},
    ).first()
    if row is None:
        return VerificationGate(False, None, 0, None)
    return VerificationGate(row[2] == "PASSED", str(row[0]), int(row[1]), row[2])


def require_verified(session: Session, target_type: str, target_id: str) -> VerificationGate:
    gate = verification_gate(session, target_type, target_id)
    if not gate.passed:
        raise CommandError(
            "VERIFICATION_REQUIRED",
            f"{target_type} {target_id} has no PASSED verification",
            status=409,
            extra={"verification_id": gate.verification_id, "result": gate.result},
        )
    return gate
