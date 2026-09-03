"""ScheduleRun subject handler for ArtifactLinks (development plan §6.8; activated in Phase 5)."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.artifacts.links import ArtifactLinkError, SubjectAcl


class ScheduleRunSubjectHandler:
    subject_type = "schedule_run"
    active = True
    activating_phase = 5

    def exists(self, session: Session, workspace_id: str, subject_id: str) -> bool:
        row = session.execute(
            text("SELECT workspace_id FROM schedule_runs WHERE run_id = :r"), {"r": subject_id}
        ).first()
        if row is None:
            return False
        if str(row[0]) != workspace_id:
            raise ArtifactLinkError("WORKSPACE_MISMATCH", "run belongs to another workspace")
        return True

    def acl(self, session: Session, workspace_id: str, subject_id: str) -> SubjectAcl:
        """Readers: the execution principal of the pinned version and the requester."""
        row = session.execute(
            text(
                "SELECT v.execution_principal_id, r.requested_by FROM schedule_runs r "
                "JOIN schedule_versions v ON v.id = r.schedule_version_id "
                "WHERE r.run_id = :r AND r.workspace_id = :ws"
            ),
            {"r": subject_id, "ws": uuid.UUID(workspace_id)},
        ).first()
        readers = {str(v) for v in (row or ()) if v is not None}
        return SubjectAcl(frozenset(readers))
