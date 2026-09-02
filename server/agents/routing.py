"""Assignment routing (development plan §7.3 runtime norms; P3-06).

The eligible set is the intersection of: active ∧ online Agents, channel membership (write),
required capability, current capacity minus load, policy allow (Policy Engine) and, when the Task
needs secret handles, adapter support for them. Score = domain match (2) + inverse recent load
(1); ties are broken by ascending ``agent_id`` so selection is reproducible (V-P3-03). Every
selection is recorded in ``routing_decisions`` and audited (``routing.select``) with the
candidate snapshot; no secret values are involved.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.clock import Clock, SystemClock
from server.observability.audit import append_audit
from server.policy.authorization import AuthorizationDenied, AuthorizationRequest

ACTIVE_TASK_STATUSES = ("DELEGATED", "ACCEPTED", "RUNNING", "WAITING", "IMPLEMENTED", "VERIFYING")


def supports_secret_handles(adapter_type: str) -> bool:
    """§7B.2: the Mattermost bot adapter advertises ``secret_handles: unsupported``.

    TODO(P3-04): switch to the transport package's helper once the default adapters land.
    """
    return adapter_type != "mattermost_bot"


@dataclass(frozen=True)
class Candidate:
    agent_id: str
    account_uuid: str
    account_id: str
    adapter_type: str
    score: int
    capacity: int
    load: int
    domain_match: bool
    reasons: tuple[str, ...] = ()

    def snapshot(self) -> dict[str, Any]:
        data = asdict(self)
        data["reasons"] = list(self.reasons)
        return data


def _policy_allows(
    authorizer: Any,
    session: Session,
    account_id: str,
    *,
    permission: str,
    channel_id: str | None,
    domain: str | None,
    required_capability: str | None,
    correlation_id: str,
) -> bool:
    """Policy allow without raising; ``None`` means no policy layer (allow, e.g. unit tests)."""
    if authorizer is None:
        return True
    engine = getattr(authorizer, "authorizer", None)
    request = AuthorizationRequest(
        permission=permission,
        channel_id=channel_id,
        domain=domain,
        required_capability=required_capability,
        correlation_id=correlation_id,
        target_type="routing",
        target_id=account_id,
    )
    if engine is not None and hasattr(engine, "authorize"):
        return bool(engine.authorize(session, account_id, request).allowed)
    try:
        authorizer.require(
            session,
            account_id,
            permission,
            channel_id=channel_id,
            domain=domain,
            capability=required_capability,
            correlation_id=correlation_id,
        )
    except (AuthorizationDenied, Exception):  # CommandError from the bus adapter
        return False
    return True


def load_of(session: Session, account_uuid: str) -> int:
    """Current load: Tasks assigned to the Account that are not terminal."""
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM tasks_projection WHERE assignee_account_id = :a "
                "AND status = ANY(:st)"
            ),
            {"a": uuid.UUID(account_uuid), "st": list(ACTIVE_TASK_STATUSES)},
        ).scalar_one()
    )


def candidates(
    session: Session,
    *,
    workspace_id: str,
    channel_uuid: str | None,
    required_capability: str | None,
    domain: str | None,
    needs_secret_handles: bool = False,
    authorizer: Any = None,
    exclude_accounts: Iterable[str] = (),
    permission: str = "task.accept",
    correlation_id: str = "-",
) -> list[Candidate]:
    """Eligible Agents ordered by score desc, agent_id asc (deterministic)."""
    excluded = {str(a) for a in exclude_accounts}
    rows = (
        session.execute(
            text(
                "SELECT ag.agent_id, ag.account_id, ac.account_id AS public_id, ag.adapter_type, "
                "ag.capacity, ag.limits, ag.online, ag.status, "
                "EXISTS (SELECT 1 FROM channel_members cm WHERE cm.account_id = ag.account_id "
                "  AND cm.channel_id = :chan AND cm.status = 'active' "
                "  AND cm.permissions ? 'write') AS member, "
                "EXISTS (SELECT 1 FROM agent_capabilities c WHERE c.agent_id = ag.agent_id "
                "  AND c.capability_id = :cap) AS has_capability, "
                "EXISTS (SELECT 1 FROM agent_capabilities c JOIN capabilities cp "
                "  ON cp.capability_id = c.capability_id WHERE c.agent_id = ag.agent_id "
                "  AND cp.domain = :dom) AS domain_match "
                "FROM agents ag JOIN accounts ac ON ac.id = ag.account_id "
                "WHERE ag.workspace_id = :ws ORDER BY ag.agent_id"
            ),
            {
                "chan": uuid.UUID(channel_uuid) if channel_uuid else None,
                "cap": required_capability,
                "dom": domain,
                "ws": uuid.UUID(workspace_id),
            },
        )
        .mappings()
        .all()
    )
    out: list[Candidate] = []
    for r in rows:
        account_uuid = str(r["account_id"])
        if account_uuid in excluded or str(r["public_id"]) in excluded:
            continue
        reasons: list[str] = []
        if r["status"] != "active":
            reasons.append(f"status:{r['status']}")
        if not r["online"]:
            reasons.append("offline")
        if channel_uuid and not r["member"]:
            reasons.append("not_member")
        if required_capability and not r["has_capability"]:
            reasons.append("capability_missing")
        if needs_secret_handles and not supports_secret_handles(str(r["adapter_type"])):
            reasons.append("secret_handles_unsupported")
        if reasons:
            continue
        limits = r["limits"] if isinstance(r["limits"], dict) else json.loads(r["limits"] or "{}")
        capacity = int(r["capacity"] or 0)
        concurrent = limits.get("concurrent_tasks")
        if isinstance(concurrent, int) and concurrent > 0:
            capacity = min(capacity, concurrent)
        load = load_of(session, account_uuid)
        if capacity - load <= 0:
            continue
        if not _policy_allows(
            authorizer,
            session,
            str(r["public_id"]),
            permission=permission,
            channel_id=channel_uuid,
            domain=domain,
            required_capability=required_capability,
            correlation_id=correlation_id,
        ):
            continue
        domain_match = bool(r["domain_match"])
        score = (2 if domain_match else 0) + (1 if load == 0 else 0)
        out.append(
            Candidate(
                agent_id=str(r["agent_id"]),
                account_uuid=account_uuid,
                account_id=str(r["public_id"]),
                adapter_type=str(r["adapter_type"]),
                score=score,
                capacity=capacity,
                load=load,
                domain_match=domain_match,
            )
        )
    out.sort(key=lambda c: (-c.score, c.agent_id))
    return out


def record_decision(
    session: Session,
    *,
    workspace_id: str,
    purpose: str,
    candidate_set: Sequence[Candidate],
    selected: Candidate | None,
    reason_code: str,
    correlation_id: str,
    actor_label: str,
    task_id: str | None = None,
    verification_id: str | None = None,
    required_capability: str | None = None,
    domain: str | None = None,
    actor_account_uuid: str | None = None,
    clock: Clock | None = None,
) -> int:
    """Persist the decision and audit it (``routing.select``); returns the decision id."""
    now = (clock or SystemClock()).now()
    audit_id = append_audit(
        session,
        action="routing.select",
        target_type="task" if task_id else "verification_run",
        target_id=task_id or verification_id or "-",
        result="SELECTED" if selected else "NO_CANDIDATE",
        actor_label=actor_label,
        correlation_id=correlation_id,
        workspace_id=uuid.UUID(workspace_id),
        actor_account_id=uuid.UUID(actor_account_uuid) if actor_account_uuid else None,
        error_code=None if selected else reason_code,
        metadata={
            "purpose": purpose,
            "candidates": [c.agent_id for c in candidate_set],
            "selected": selected.agent_id if selected else None,
            "required_capability": required_capability,
            "domain": domain,
        },
        clock=clock,
    )
    row = session.execute(
        text(
            "INSERT INTO routing_decisions (workspace_id, purpose, task_id, verification_id, "
            "required_capability, domain, candidates, selected_account_id, selected_agent_id, "
            "reason_code, correlation_id, decided_at, audit_id) VALUES (:ws, :p, :t, :v, :cap, "
            ":dom, CAST(:c AS jsonb), :sa, :sg, :r, :corr, :now, :aid) RETURNING id"
        ),
        {
            "ws": uuid.UUID(workspace_id),
            "p": purpose,
            "t": task_id,
            "v": verification_id,
            "cap": required_capability,
            "dom": domain,
            "c": json.dumps([c.snapshot() for c in candidate_set]),
            "sa": uuid.UUID(selected.account_uuid) if selected else None,
            "sg": selected.agent_id if selected else None,
            "r": reason_code,
            "corr": correlation_id,
            "now": now,
            "aid": audit_id,
        },
    ).first()
    assert row is not None
    return int(row[0])


def select_assignee(
    session: Session,
    *,
    workspace_id: str,
    task_id: str,
    channel_uuid: str | None,
    required_capability: str | None,
    domain: str | None,
    correlation_id: str,
    actor_label: str,
    purpose: str = "assignment",
    needs_secret_handles: bool = False,
    authorizer: Any = None,
    exclude_accounts: Iterable[str] = (),
    actor_account_uuid: str | None = None,
    clock: Clock | None = None,
) -> Candidate | None:
    """Pick the best eligible Agent for a Task and record the decision (None when none)."""
    found = candidates(
        session,
        workspace_id=workspace_id,
        channel_uuid=channel_uuid,
        required_capability=required_capability,
        domain=domain,
        needs_secret_handles=needs_secret_handles,
        authorizer=authorizer,
        exclude_accounts=exclude_accounts,
        correlation_id=correlation_id,
    )
    chosen = found[0] if found else None
    record_decision(
        session,
        workspace_id=workspace_id,
        purpose=purpose,
        candidate_set=found,
        selected=chosen,
        reason_code="SELECTED" if chosen else "NO_CANDIDATE",
        correlation_id=correlation_id,
        actor_label=actor_label,
        task_id=task_id,
        required_capability=required_capability,
        domain=domain,
        actor_account_uuid=actor_account_uuid,
        clock=clock,
    )
    return chosen


def decisions_for(session: Session, task_id: str) -> list[dict[str, Any]]:
    rows = (
        session.execute(
            text(
                "SELECT purpose, candidates, selected_agent_id, reason_code, decided_at "
                "FROM routing_decisions WHERE task_id = :t ORDER BY id"
            ),
            {"t": task_id},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def utc(ts: dt.datetime) -> str:
    return ts.astimezone(dt.UTC).isoformat()
