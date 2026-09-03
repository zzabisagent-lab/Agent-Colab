"""ArtifactLink subject-handler registry (spec §9.2, development plan §6.8).

Subject types are fixed to ``task | schedule_run | brainstorm | decision``. A handler activates
in the phase that introduces the entity: ``task`` in Phase 1, ``schedule_run`` in Phase 5,
``brainstorm``/``decision`` in Phase 6. Inactive handlers return the stable error
``SUBJECT_TYPE_NOT_ACTIVE`` and create no side effects.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

SUBJECT_TYPES: tuple[str, ...] = ("task", "schedule_run", "brainstorm", "decision")


class ArtifactLinkError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SubjectAcl:
    """Accounts (UUID strings) that may read artifacts linked to the subject."""

    readers: frozenset[str]


class SubjectHandler(Protocol):
    subject_type: str
    active: bool
    activating_phase: int

    def exists(self, session: Session, workspace_id: str, subject_id: str) -> bool: ...

    def acl(self, session: Session, workspace_id: str, subject_id: str) -> SubjectAcl: ...


class TaskSubjectHandler:
    subject_type = "task"
    active = True
    activating_phase = 1

    def exists(self, session: Session, workspace_id: str, subject_id: str) -> bool:
        row = session.execute(
            text("SELECT workspace_id FROM tasks_projection WHERE task_id = :t"), {"t": subject_id}
        ).first()
        if row is None:
            return False
        if str(row[0]) != workspace_id:
            raise ArtifactLinkError("WORKSPACE_MISMATCH", "task belongs to another workspace")
        return True

    def acl(self, session: Session, workspace_id: str, subject_id: str) -> SubjectAcl:
        row = session.execute(
            text(
                "SELECT assignee_account_id, delegated_by FROM tasks_projection "
                "WHERE task_id = :t AND workspace_id = :ws"
            ),
            {"t": subject_id, "ws": uuid.UUID(workspace_id)},
        ).first()
        readers = {str(v) for v in (row or ()) if v is not None}
        return SubjectAcl(frozenset(readers))


class InactiveSubjectHandler:
    active = False

    def __init__(self, subject_type: str, activating_phase: int) -> None:
        self.subject_type = subject_type
        self.activating_phase = activating_phase

    def _inactive(self) -> ArtifactLinkError:
        return ArtifactLinkError(
            "SUBJECT_TYPE_NOT_ACTIVE",
            f"{self.subject_type} links activate in Phase {self.activating_phase}",
        )

    def exists(self, session: Session, workspace_id: str, subject_id: str) -> bool:
        raise self._inactive()

    def acl(self, session: Session, workspace_id: str, subject_id: str) -> SubjectAcl:
        raise self._inactive()


class SubjectRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, SubjectHandler] = {}

    def register(self, handler: SubjectHandler) -> None:
        if handler.subject_type not in SUBJECT_TYPES:
            raise ArtifactLinkError("SUBJECT_TYPE_UNKNOWN", handler.subject_type)
        self._handlers[handler.subject_type] = handler

    def get(self, subject_type: str) -> SubjectHandler:
        handler = self._handlers.get(subject_type)
        if handler is None:
            raise ArtifactLinkError("SUBJECT_TYPE_UNKNOWN", subject_type)
        return handler

    def require_active(self, subject_type: str) -> SubjectHandler:
        handler = self.get(subject_type)
        if not handler.active:
            raise ArtifactLinkError(
                "SUBJECT_TYPE_NOT_ACTIVE",
                f"{subject_type} links activate in Phase {handler.activating_phase}",
            )
        return handler

    def status(self) -> dict[str, dict[str, object]]:
        return {
            t: {"active": h.active, "activating_phase": h.activating_phase}
            for t, h in self._handlers.items()
        }


def default_registry() -> SubjectRegistry:
    registry = SubjectRegistry()
    registry.register(TaskSubjectHandler())
    from server.schedules.links import ScheduleRunSubjectHandler  # Phase 5 activation (§6.8)

    registry.register(ScheduleRunSubjectHandler())
    from server.artifacts.subjects_phase6 import (  # Phase 6 activation (§6.8)
        BrainstormSubjectHandler,
        DecisionSubjectHandler,
    )

    registry.register(BrainstormSubjectHandler())
    registry.register(DecisionSubjectHandler())
    return registry


REGISTRY = default_registry()


def link_artifact(
    session: Session,
    *,
    workspace_id: str,
    artifact_id: str,
    subject_type: str,
    subject_id: str,
    relation: str,
    linked_by: str,
    registry: SubjectRegistry | None = None,
) -> None:
    """Insert an artifact_links row after existence, workspace, activation, uniqueness checks."""
    reg = registry or REGISTRY
    handler = reg.require_active(subject_type)
    if not relation or len(relation) > 64:
        raise ArtifactLinkError("ARTIFACT_LINK_RELATION_INVALID", "relation must be 1-64 chars")
    art = session.execute(
        text("SELECT workspace_id FROM artifacts WHERE artifact_id = :a"), {"a": artifact_id}
    ).first()
    if art is None:
        raise ArtifactLinkError("ARTIFACT_NOT_FOUND", artifact_id)
    if str(art[0]) != workspace_id:
        raise ArtifactLinkError("WORKSPACE_MISMATCH", "artifact belongs to another workspace")
    if not handler.exists(session, workspace_id, subject_id):
        raise ArtifactLinkError("SUBJECT_NOT_FOUND", f"{subject_type} {subject_id}")
    dup = session.execute(
        text(
            "SELECT 1 FROM artifact_links WHERE artifact_id = :a AND subject_type = :t "
            "AND subject_id = :s AND relation = :r"
        ),
        {"a": artifact_id, "t": subject_type, "s": subject_id, "r": relation},
    ).first()
    if dup is not None:
        raise ArtifactLinkError("ARTIFACT_LINK_DUPLICATE", "link already exists")
    session.execute(
        text(
            "INSERT INTO artifact_links "
            "(artifact_id, subject_type, subject_id, relation, linked_by) "
            "VALUES (:a, :t, :s, :r, :b)"
        ),
        {
            "a": artifact_id,
            "t": subject_type,
            "s": subject_id,
            "r": relation,
            "b": uuid.UUID(linked_by),
        },
    )


def linked_readers(
    session: Session,
    workspace_id: str,
    artifact_id: str,
    registry: SubjectRegistry | None = None,
) -> frozenset[str]:
    """Union of subject ACL readers over every active link of the artifact."""
    reg = registry or REGISTRY
    rows = session.execute(
        text("SELECT subject_type, subject_id FROM artifact_links WHERE artifact_id = :a"),
        {"a": artifact_id},
    ).all()
    readers: set[str] = set()
    for subject_type, subject_id in rows:
        handler = reg.get(str(subject_type))
        if handler.active:
            readers |= handler.acl(session, workspace_id, str(subject_id)).readers
    return frozenset(readers)
