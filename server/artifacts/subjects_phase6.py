"""Brainstorm and Decision ArtifactLink handlers (P6-03; development plan §6.8, spec §9.2).

These two subject types activate in Phase 6. The handlers read only the columns the Brainstorm
package (P6-02/P6-09) owns:

``brainstorms(brainstorm_id, workspace_id, facilitator_account_id)``
``brainstorm_participants(brainstorm_id, account_id)``
``brainstorm_decisions(decision_id, workspace_id, brainstorm_id, decided_by)``

A subject whose table has not been created yet reports ``SUBJECT_NOT_FOUND`` rather than raising a
database error, so linking degrades to "unknown id" instead of a 500 while the packages land.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session

from server.artifacts.links import ArtifactLinkError, SubjectAcl


def _table_exists(session: Session, name: str) -> bool:
    return bool(session.execute(text("SELECT to_regclass(:n)"), {"n": name}).scalar())


class BrainstormSubjectHandler:
    subject_type = "brainstorm"
    active = True
    activating_phase = 6

    def exists(self, session: Session, workspace_id: str, subject_id: str) -> bool:
        if not _table_exists(session, "brainstorms"):
            return False
        row = session.execute(
            text("SELECT workspace_id FROM brainstorms WHERE brainstorm_id = :b"),
            {"b": subject_id},
        ).first()
        if row is None:
            return False
        if str(row[0]) != workspace_id:
            raise ArtifactLinkError("WORKSPACE_MISMATCH", "brainstorm belongs to another workspace")
        return True

    def acl(self, session: Session, workspace_id: str, subject_id: str) -> SubjectAcl:
        """Readers: the facilitator and every participant of the session."""
        readers: set[str] = set()
        if not _table_exists(session, "brainstorms"):
            return SubjectAcl(frozenset())
        row = session.execute(
            text(
                "SELECT facilitator_account_id FROM brainstorms "
                "WHERE brainstorm_id = :b AND workspace_id = :ws"
            ),
            {"b": subject_id, "ws": uuid.UUID(workspace_id)},
        ).first()
        if row and row[0] is not None:
            readers.add(str(row[0]))
        if _table_exists(session, "brainstorm_participants"):
            try:
                rows = session.execute(
                    text("SELECT account_id FROM brainstorm_participants WHERE brainstorm_id = :b"),
                    {"b": subject_id},
                ).all()
            except DatabaseError:  # a differently shaped participants table: facilitator only
                rows = []
            readers |= {str(r[0]) for r in rows if r[0] is not None}
        return SubjectAcl(frozenset(readers))


class DecisionSubjectHandler:
    subject_type = "decision"
    active = True
    activating_phase = 6

    def exists(self, session: Session, workspace_id: str, subject_id: str) -> bool:
        if not _table_exists(session, "brainstorm_decisions"):
            return False
        row = session.execute(
            text("SELECT workspace_id FROM brainstorm_decisions WHERE decision_id = :d"),
            {"d": subject_id},
        ).first()
        if row is None:
            return False
        if str(row[0]) != workspace_id:
            raise ArtifactLinkError("WORKSPACE_MISMATCH", "decision belongs to another workspace")
        return True

    def acl(self, session: Session, workspace_id: str, subject_id: str) -> SubjectAcl:
        """Readers: whoever recorded the Decision, plus the readers of its Brainstorm."""
        if not _table_exists(session, "brainstorm_decisions"):
            return SubjectAcl(frozenset())
        row = session.execute(
            text(
                "SELECT decided_by, brainstorm_id FROM brainstorm_decisions "
                "WHERE decision_id = :d AND workspace_id = :ws"
            ),
            {"d": subject_id, "ws": uuid.UUID(workspace_id)},
        ).first()
        if row is None:
            return SubjectAcl(frozenset())
        readers = {str(row[0])} if row[0] is not None else set()
        if row[1]:
            readers |= BrainstormSubjectHandler().acl(session, workspace_id, str(row[1])).readers
        return SubjectAcl(frozenset(readers))
