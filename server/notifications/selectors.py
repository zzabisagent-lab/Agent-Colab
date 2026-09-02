"""Recipient selectors (development plan §7G). Each selector resolves an Event to a sorted,
deduplicated list of Account UUIDs from committed rows only (never from projections of state
that decides permissions; the approver set is computed from committed RoleVersions)."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.policy.model import permission_matches

Event = dict[str, Any]
Selector = Callable[[Session, Event, dt.datetime], list[str]]

_ACTIVE_ROLE_HOLDERS = text(
    "SELECT a.id, a.account_type, rv.permissions, rv.deny FROM accounts a "
    "JOIN principal_role_assignments pra ON pra.account_id = a.id AND pra.revoked_at IS NULL "
    "  AND pra.valid_from <= :now AND (pra.valid_to IS NULL OR pra.valid_to > :now) "
    "JOIN roles r ON r.role_id = pra.role_id AND r.status = 'active' "
    "JOIN role_versions rv ON rv.role_id = r.role_id AND rv.version = r.current_version "
    "WHERE a.workspace_id = :ws AND a.status = 'ACTIVE'"
)


def _grants(permissions: Any, deny: Any, permission: str) -> bool:
    perms = permissions if isinstance(permissions, list) else json.loads(permissions or "[]")
    denies = deny if isinstance(deny, list) else json.loads(deny or "[]")
    if any(permission_matches(str(p), permission) for p in denies):
        return False
    return any(permission_matches(str(p), permission) for p in perms)


def _sorted(ids: set[str]) -> list[str]:
    return sorted(ids)


def _account_uuid(session: Session, ref: str | None) -> str | None:
    """Accept either an Account UUID or a public account_id and return the UUID as text."""
    if not ref:
        return None
    try:
        uuid.UUID(ref)
        row = session.execute(
            text("SELECT id FROM accounts WHERE id = :i"), {"i": uuid.UUID(ref)}
        ).first()
    except ValueError:
        row = session.execute(
            text("SELECT id FROM accounts WHERE account_id = :a"), {"a": ref}
        ).first()
    return None if row is None else str(row[0])


def _channel_members(session: Session, channel_uuid: str | None) -> set[str]:
    if not channel_uuid:
        return set()
    rows = session.execute(
        text("SELECT account_id FROM channel_members WHERE channel_id = :c AND status = 'active'"),
        {"c": uuid.UUID(channel_uuid)},
    ).all()
    return {str(r[0]) for r in rows}


def _channels_of_type(session: Session, workspace_uuid: str, channel_type: str) -> list[str]:
    rows = session.execute(
        text(
            "SELECT id FROM channels WHERE workspace_id = :ws AND channel_type = :t "
            "AND status = 'active'"
        ),
        {"ws": uuid.UUID(workspace_uuid), "t": channel_type},
    ).all()
    return [str(r[0]) for r in rows]


def eligible_approvers(session: Session, event: Event, now: dt.datetime) -> list[str]:
    payload = event.get("payload", {})
    approval_id = payload.get("approval_id")
    risk = str(payload.get("risk", "LOW"))
    channel_uuid = event.get("channel_id")
    excluded: set[str] = {str(event["actor_account_id"])}
    if approval_id:
        grant = session.execute(
            text(
                "SELECT requested_by, implementing_agent_account_id, channel_id, risk "
                "FROM approval_grants WHERE approval_id = :a"
            ),
            {"a": approval_id},
        ).first()
        if grant is not None:
            excluded.add(str(grant[0]))
            if grant[1] is not None:
                excluded.add(str(grant[1]))
            channel_uuid = str(grant[2]) if grant[2] is not None else channel_uuid
            risk = str(grant[3])
    if payload.get("implementing_agent_account_id"):
        impl = _account_uuid(session, str(payload["implementing_agent_account_id"]))
        if impl:
            excluded.add(impl)
    members = _channel_members(session, channel_uuid)
    human_only = risk in ("HIGH", "CRITICAL")
    out: set[str] = set()
    for acc_id, acc_type, perms, deny in session.execute(
        _ACTIVE_ROLE_HOLDERS, {"now": now, "ws": uuid.UUID(str(event["workspace_id"]))}
    ).all():
        sid = str(acc_id)
        if sid in excluded or sid not in members:
            continue
        if human_only and acc_type != "human":
            continue
        if _grants(perms, deny, "approval.decide"):
            out.add(sid)
    return _sorted(out)


def verifier(session: Session, event: Event, now: dt.datetime) -> list[str]:
    acc = _account_uuid(session, event.get("payload", {}).get("verifier_account_id"))
    return [acc] if acc else []


def delegator(session: Session, event: Event, now: dt.datetime) -> list[str]:
    payload = event.get("payload", {})
    task_id = event.get("task_id") or payload.get("task_id")
    if task_id:
        row = session.execute(
            text("SELECT delegated_by FROM tasks_projection WHERE task_id = :t"), {"t": task_id}
        ).first()
        if row is not None and row[0] is not None:
            return [str(row[0])]
    acc = _account_uuid(session, payload.get("delegator_account_id"))
    return [acc] if acc else []


def channel_members(session: Session, event: Event, now: dt.datetime) -> list[str]:
    return _sorted(_channel_members(session, event.get("channel_id")))


def ops_channel_members(session: Session, event: Event, now: dt.datetime) -> list[str]:
    out: set[str] = set()
    for ch in _channels_of_type(session, str(event["workspace_id"]), "ops"):
        out |= _channel_members(session, ch)
    return _sorted(out)


def administrators(session: Session, event: Event, now: dt.datetime) -> list[str]:
    out: set[str] = set()
    for acc_id, acc_type, perms, deny in session.execute(
        _ACTIVE_ROLE_HOLDERS, {"now": now, "ws": uuid.UUID(str(event["workspace_id"]))}
    ).all():
        if acc_type == "human" and _grants(perms, deny, "admin.settings"):
            out.add(str(acc_id))
    return _sorted(out)


def agent_owner(session: Session, event: Event, now: dt.datetime) -> list[str]:
    payload = event.get("payload", {})
    owner = _account_uuid(session, payload.get("owner_account_id"))
    if owner:
        return [owner]
    agent_id = payload.get("agent_id")
    if agent_id:
        row = session.execute(
            text("SELECT account_id FROM agents WHERE agent_id = :a"), {"a": agent_id}
        ).first()
        if row is not None:
            return [str(row[0])]
    return []


SELECTORS: dict[str, Selector] = {
    "eligible_approvers": eligible_approvers,
    "verifier": verifier,
    "delegator": delegator,
    "channel_members": channel_members,
    "ops_channel_members": ops_channel_members,
    "administrators": administrators,
    "agent_owner": agent_owner,
}


def resolve_recipients(
    session: Session, selector: str, event: Event, now: dt.datetime
) -> list[str]:
    fn = SELECTORS.get(selector)
    if fn is None:
        raise KeyError(f"unknown recipient selector {selector}")
    return fn(session, event, now)


def channel_destinations(session: Session, event: Event) -> dict[str, list[str]]:
    """Channel-level destinations (posts, not per-recipient): channel UUIDs by channel kind."""
    ws = str(event["workspace_id"])
    return {
        "mattermost:channel": [str(event["channel_id"])] if event.get("channel_id") else [],
        "mattermost:approval_channel": _channels_of_type(session, ws, "approval"),
        "mattermost:ops_channel": _channels_of_type(session, ws, "ops"),
    }
