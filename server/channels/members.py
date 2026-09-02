"""Channel membership and per-channel configuration helpers (P2-02; spec §8.1, §9.1).

Membership rows live in ``channel_members`` (Humans, Agents, and services all join through their
Account). Permissions are a subset of ``read``, ``write``, ``moderate``. Every change is applied
per channel only: nothing here touches another channel's rows.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

MEMBER_PERMISSIONS = ("read", "write", "moderate")


class MembershipError(ValueError):
    def __init__(self, code: str, detail: str, status: int = 409) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = status


@dataclass(frozen=True)
class ChannelRef:
    id: uuid.UUID
    channel_id: str
    status: str
    documentation_template: str | None
    policy: dict[str, Any]


@dataclass(frozen=True)
class Member:
    account_id: str
    account_uuid: uuid.UUID
    account_type: str
    permissions: tuple[str, ...]
    status: str


def validate_permissions(permissions: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    perms = tuple(dict.fromkeys(str(p) for p in permissions))
    unknown = [p for p in perms if p not in MEMBER_PERMISSIONS]
    if unknown or not perms:
        raise MembershipError(
            "MEMBER_PERMISSIONS_INVALID", f"allowed {MEMBER_PERMISSIONS}, got {list(perms)}", 422
        )
    if "read" not in perms:
        perms = ("read", *perms)
    return perms


def channel_ref(session: Session, workspace_id: uuid.UUID, channel_id: str) -> ChannelRef:
    row = (
        session.execute(
            text(
                "SELECT id, channel_id, status, documentation_template, policy FROM channels "
                "WHERE channel_id = :c AND workspace_id = :ws FOR UPDATE"
            ),
            {"c": channel_id, "ws": workspace_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise MembershipError("CHANNEL_NOT_FOUND", channel_id, 404)
    policy = row["policy"] if isinstance(row["policy"], dict) else json.loads(row["policy"] or "{}")
    return ChannelRef(
        uuid.UUID(str(row["id"])),
        str(row["channel_id"]),
        str(row["status"]),
        row["documentation_template"],
        policy,
    )


def account_ref(
    session: Session, workspace_id: uuid.UUID, account_id: str
) -> tuple[uuid.UUID, str]:
    row = session.execute(
        text(
            "SELECT id, account_type FROM accounts WHERE account_id = :a AND workspace_id = :ws "
            "AND status = 'ACTIVE'"
        ),
        {"a": account_id, "ws": workspace_id},
    ).first()
    if row is None:
        raise MembershipError("ACCOUNT_NOT_FOUND", account_id, 404)
    return uuid.UUID(str(row[0])), str(row[1])


def list_members(session: Session, channel_uuid: uuid.UUID) -> list[Member]:
    rows = session.execute(
        text(
            "SELECT a.account_id, a.id, a.account_type, m.permissions, m.status "
            "FROM channel_members m JOIN accounts a ON a.id = m.account_id "
            "WHERE m.channel_id = :c ORDER BY a.account_id"
        ),
        {"c": channel_uuid},
    ).all()
    out: list[Member] = []
    for account_id, acc_uuid, typ, perms, status in rows:
        perms_list = perms if isinstance(perms, list) else json.loads(perms)
        out.append(
            Member(str(account_id), uuid.UUID(str(acc_uuid)), str(typ), tuple(perms_list), status)
        )
    return out


def add_member(
    session: Session, channel_uuid: uuid.UUID, account_uuid: uuid.UUID, permissions: tuple[str, ...]
) -> None:
    session.execute(
        text(
            "INSERT INTO channel_members (channel_id, account_id, permissions, status) "
            "VALUES (:c, :a, CAST(:p AS jsonb), 'active') ON CONFLICT (channel_id, account_id) "
            "DO UPDATE SET permissions = EXCLUDED.permissions, status = 'active'"
        ),
        {"c": channel_uuid, "a": account_uuid, "p": json.dumps(list(permissions))},
    )


def remove_member(session: Session, channel_uuid: uuid.UUID, account_uuid: uuid.UUID) -> bool:
    result = session.execute(
        text(
            "UPDATE channel_members SET status = 'removed' "
            "WHERE channel_id = :c AND account_id = :a AND status = 'active'"
        ),
        {"c": channel_uuid, "a": account_uuid},
    )
    return bool(getattr(result, "rowcount", 0))


def set_permissions(
    session: Session, channel_uuid: uuid.UUID, account_uuid: uuid.UUID, permissions: tuple[str, ...]
) -> bool:
    result = session.execute(
        text(
            "UPDATE channel_members SET permissions = CAST(:p AS jsonb) WHERE channel_id = :c "
            "AND account_id = :a AND status = 'active'"
        ),
        {"c": channel_uuid, "a": account_uuid, "p": json.dumps(list(permissions))},
    )
    return bool(getattr(result, "rowcount", 0))


def set_document_template(session: Session, channel_uuid: uuid.UUID, template: str | None) -> None:
    session.execute(
        text("UPDATE channels SET documentation_template = :t WHERE id = :c"),
        {"t": template, "c": channel_uuid},
    )


def is_active_member(session: Session, channel_uuid: uuid.UUID, account_uuid: uuid.UUID) -> bool:
    row = session.execute(
        text(
            "SELECT 1 FROM channel_members WHERE channel_id = :c AND account_id = :a "
            "AND status = 'active'"
        ),
        {"c": channel_uuid, "a": account_uuid},
    ).first()
    return row is not None
