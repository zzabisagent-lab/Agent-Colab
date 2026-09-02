"""Committed policy state for principals (P1-03).

Reads only authority rows: active ``principal_role_assignments`` (valid window, not revoked) →
active ``roles`` → the role's **current committed** ``role_versions`` row; Agent capabilities via
``agent_capabilities``; channel membership via ``channel_members``. Projections are never used.
Role changes are versioned: ``commit_role_version`` appends an immutable ``role_versions`` row and
advances ``roles.current_version`` in the same transaction (spec §4.2, development plan §6.7).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.events.canonical import canonical_json
from server.policy.model import Constraints, Role


class PolicyRepositoryError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class PrincipalInfo:
    account_id: str
    account_uuid: uuid.UUID
    workspace_uuid: uuid.UUID
    account_type: str
    status: str
    agent_id: str | None


@dataclass(frozen=True)
class PolicySnapshot:
    """Pinnable view of the committed policy a decision was computed from (spec §4.2)."""

    account_id: str
    role_versions: tuple[tuple[str, int], ...]
    capability_ids: tuple[str, ...]
    policy_hash: str
    computed_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "role_versions": [list(rv) for rv in self.role_versions],
            "capability_ids": list(self.capability_ids),
            "policy_hash": self.policy_hash,
            "computed_at": self.computed_at,
        }


def policy_hash(permissions: list[str], deny: list[str], constraints: dict[str, Any]) -> str:
    body = {"permissions": sorted(permissions), "deny": sorted(deny), "constraints": constraints}
    return hashlib.sha256(canonical_json(body)).hexdigest()


def role_from_version(
    role_id: str,
    version: int,
    permissions: list[str],
    deny: list[str],
    constraints: dict[str, Any],
    status: str = "active",
) -> Role:
    c = constraints or {}
    return Role(
        role_id=role_id,
        version=version,
        permissions=frozenset(permissions),
        deny=frozenset(deny),
        constraints=Constraints(
            domains=frozenset(c["domains"]) if c.get("domains") is not None else None,
            side_effects=str(c.get("side_effects", "allow")),
            requires_human_approval=frozenset(c.get("requires_human_approval", [])),
            channels=frozenset(c["channels"]) if c.get("channels") is not None else None,
            resources=frozenset(c["resources"]) if c.get("resources") is not None else None,
            max_risk=str(c.get("max_risk", "CRITICAL")),
        ),
        status=status,
    )


def snapshot_of(
    account_id: str, roles: list[Role], capabilities: frozenset[str], now: dt.datetime
) -> PolicySnapshot:
    role_versions = tuple(sorted((r.role_id, r.version) for r in roles))
    caps = tuple(sorted(capabilities))
    digest = hashlib.sha256(
        canonical_json(
            {
                "account_id": account_id,
                "role_versions": [list(rv) for rv in role_versions],
                "capability_ids": list(caps),
            }
        )
    ).hexdigest()
    return PolicySnapshot(account_id, role_versions, caps, digest, now.isoformat())


class PolicyRepository(Protocol):
    def principal(self, session: Session, account_id: str) -> PrincipalInfo | None: ...

    def effective_roles(
        self, session: Session, principal: PrincipalInfo, now: dt.datetime
    ) -> list[Role]: ...

    def capability_ids(self, session: Session, principal: PrincipalInfo) -> frozenset[str]: ...

    def is_channel_member(
        self, session: Session, principal: PrincipalInfo, channel_id: str
    ) -> bool: ...


class PostgresPolicyRepository:
    """Authority-row reader/writer for policy state."""

    def principal(self, session: Session, account_id: str) -> PrincipalInfo | None:
        row = session.execute(
            text(
                "SELECT a.account_id, a.id, a.workspace_id, a.account_type, a.status, ag.agent_id "
                "FROM accounts a LEFT JOIN agents ag ON ag.account_id = a.id "
                "WHERE a.account_id = :a"
            ),
            {"a": account_id},
        ).first()
        if row is None:
            return None
        return PrincipalInfo(
            account_id=str(row[0]),
            account_uuid=uuid.UUID(str(row[1])),
            workspace_uuid=uuid.UUID(str(row[2])),
            account_type=str(row[3]),
            status=str(row[4]),
            agent_id=None if row[5] is None else str(row[5]),
        )

    def effective_roles(
        self, session: Session, principal: PrincipalInfo, now: dt.datetime
    ) -> list[Role]:
        rows = session.execute(
            text(
                "SELECT r.role_id, rv.version, rv.permissions, rv.deny, rv.constraints, r.status "
                "FROM principal_role_assignments pra "
                "JOIN roles r ON r.role_id = pra.role_id "
                "JOIN role_versions rv ON rv.role_id = r.role_id "
                "AND rv.version = r.current_version "
                "WHERE pra.account_id = :acct AND pra.revoked_at IS NULL "
                "AND pra.valid_from <= :now AND (pra.valid_to IS NULL OR pra.valid_to > :now) "
                "AND r.status = 'active' AND r.current_version > 0 "
                "ORDER BY r.role_id"
            ),
            {"acct": principal.account_uuid, "now": now},
        ).all()
        return [
            role_from_version(str(r[0]), int(r[1]), list(r[2]), list(r[3]), dict(r[4]), str(r[5]))
            for r in rows
        ]

    def capability_ids(self, session: Session, principal: PrincipalInfo) -> frozenset[str]:
        if principal.agent_id is None:
            return frozenset()
        rows = session.execute(
            text("SELECT capability_id FROM agent_capabilities WHERE agent_id = :a"),
            {"a": principal.agent_id},
        ).all()
        return frozenset(str(r[0]) for r in rows)

    def is_channel_member(
        self, session: Session, principal: PrincipalInfo, channel_id: str
    ) -> bool:
        row = session.execute(
            text(
                "SELECT 1 FROM channel_members cm JOIN channels c ON c.id = cm.channel_id "
                "WHERE cm.account_id = :acct AND cm.status = 'active' "
                "AND (c.channel_id = :cid OR c.id::text = :cid) LIMIT 1"
            ),
            {"acct": principal.account_uuid, "cid": channel_id},
        ).first()
        return row is not None

    # ------------------------------------------------------------------ writers
    def create_role(
        self, session: Session, workspace_uuid: uuid.UUID, role_id: str, display_name: str
    ) -> uuid.UUID:
        rid = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO roles (id, role_id, workspace_id, display_name) "
                "VALUES (:id, :role_id, :ws, :name)"
            ),
            {"id": rid, "role_id": role_id, "ws": workspace_uuid, "name": display_name},
        )
        return rid

    def commit_role_version(
        self,
        session: Session,
        role_id: str,
        permissions: list[str],
        deny: list[str],
        constraints: dict[str, Any],
        created_by: uuid.UUID,
        event_id: str | None = None,
    ) -> tuple[int, str]:
        """Append an immutable role version and advance ``roles.current_version`` atomically."""
        row = session.execute(
            text("SELECT current_version FROM roles WHERE role_id = :r FOR UPDATE"),
            {"r": role_id},
        ).first()
        if row is None:
            raise PolicyRepositoryError("ROLE_NOT_FOUND", role_id)
        version = int(row[0]) + 1
        digest = policy_hash(permissions, deny, constraints)
        session.execute(
            text(
                "INSERT INTO role_versions (id, role_id, version, permissions, deny, constraints, "
                "policy_hash, event_id, created_by) VALUES (:id, :role_id, :version, "
                "CAST(:permissions AS jsonb), CAST(:deny AS jsonb), CAST(:constraints AS jsonb), "
                ":hash, :event_id, :created_by)"
            ),
            {
                "id": uuid.uuid4(),
                "role_id": role_id,
                "version": version,
                "permissions": json.dumps(sorted(permissions)),
                "deny": json.dumps(sorted(deny)),
                "constraints": json.dumps(constraints),
                "hash": digest,
                "event_id": event_id,
                "created_by": created_by,
            },
        )
        session.execute(
            text("UPDATE roles SET current_version = :v WHERE role_id = :r"),
            {"v": version, "r": role_id},
        )
        return version, digest

    def assign_role(
        self,
        session: Session,
        account_uuid: uuid.UUID,
        role_id: str,
        assigned_by: uuid.UUID,
        valid_from: dt.datetime,
        valid_to: dt.datetime | None = None,
        scope: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> uuid.UUID:
        aid = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO principal_role_assignments (id, account_id, role_id, scope, "
                "valid_from, valid_to, assigned_by, event_id) VALUES (:id, :acct, :role, "
                "CAST(:scope AS jsonb), "
                ":vf, :vt, :by, :ev)"
            ),
            {
                "id": aid,
                "acct": account_uuid,
                "role": role_id,
                "scope": json.dumps(scope or {}),
                "vf": valid_from,
                "vt": valid_to,
                "by": assigned_by,
                "ev": event_id,
            },
        )
        return aid

    def revoke_role(
        self,
        session: Session,
        account_uuid: uuid.UUID,
        role_id: str,
        now: dt.datetime,
        revoke_event_id: str | None = None,
    ) -> int:
        result = session.execute(
            text(
                "UPDATE principal_role_assignments SET revoked_at = :now, revoke_event_id = :ev "
                "WHERE account_id = :acct AND role_id = :role AND revoked_at IS NULL"
            ),
            {"now": now, "ev": revoke_event_id, "acct": account_uuid, "role": role_id},
        )
        return int(getattr(result, "rowcount", 0) or 0)

    def grant_capability(self, session: Session, agent_id: str, capability_id: str) -> None:
        session.execute(
            text(
                "INSERT INTO agent_capabilities (agent_id, capability_id) VALUES (:a, :c) "
                "ON CONFLICT DO NOTHING"
            ),
            {"a": agent_id, "c": capability_id},
        )
