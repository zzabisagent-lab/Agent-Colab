"""Artifact queries and ACL decisions shared by commands and transports."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.artifacts.links import SubjectRegistry, linked_readers


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    workspace_id: str
    creator_account_id: str
    storage_uri: str
    mime: str
    size: int
    sha256: str
    acl: dict[str, Any]
    status: str
    source_event_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "workspace_id": self.workspace_id,
            "creator_account_id": self.creator_account_id,
            "storage_uri": self.storage_uri,
            "mime": self.mime,
            "size": self.size,
            "sha256": self.sha256,
            "acl": self.acl,
            "status": self.status,
            "source_event_id": self.source_event_id,
        }


def get_artifact(session: Session, workspace_id: str, artifact_id: str) -> ArtifactRecord | None:
    row = session.execute(
        text(
            "SELECT artifact_id, workspace_id, creator_account_id, storage_uri, mime, size, "
            "sha256, acl, status, source_event_id FROM artifacts "
            "WHERE artifact_id = :a AND workspace_id = :ws"
        ),
        {"a": artifact_id, "ws": uuid.UUID(workspace_id)},
    ).first()
    if row is None:
        return None
    acl = row[7] if isinstance(row[7], dict) else json.loads(row[7] or "{}")
    return ArtifactRecord(
        str(row[0]),
        str(row[1]),
        str(row[2]),
        str(row[3]),
        str(row[4]),
        int(row[5]),
        str(row[6]),
        acl,
        str(row[8]),
        str(row[9]),
    )


def can_read(
    session: Session,
    record: ArtifactRecord,
    principal_account_uuid: str,
    *,
    workspace_admin: bool = False,
    registry: SubjectRegistry | None = None,
) -> bool:
    """Creator, explicitly listed readers, readers of linked subjects, or workspace admins."""
    if workspace_admin or principal_account_uuid == record.creator_account_id:
        return True
    if principal_account_uuid in set(record.acl.get("readers", [])):
        return True
    return principal_account_uuid in linked_readers(
        session, record.workspace_id, record.artifact_id, registry
    )


def links_of(session: Session, artifact_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            "SELECT subject_type, subject_id, relation, linked_by, linked_at FROM artifact_links "
            "WHERE artifact_id = :a ORDER BY linked_at"
        ),
        {"a": artifact_id},
    ).all()
    return [
        {
            "subject_type": r[0],
            "subject_id": r[1],
            "relation": r[2],
            "linked_by": str(r[3]),
            "linked_at": r[4].isoformat(),
        }
        for r in rows
    ]
