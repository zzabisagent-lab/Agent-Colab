"""Per-Run authority re-check (development plan §10A.2 step 4, §10A.4; P5-04).

Evaluated on every Run against the *live* state while the action/budget/documentation snapshot
comes from the pinned ScheduleVersion: Schedule status, execution principal status and Roles,
Channel membership, Agent selection (fixed Agent or capability query), Approval requirement and
Secret grants. A failed check yields a stable skip code and zero side effects.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from server.agents import routing
from server.application import bus
from server.approvals.model import Subject
from server.identity.principals import Principal
from server.schedules.contract import ScheduleStatus, SkipCode

if TYPE_CHECKING:
    from server.schedules.execution import ExecutionContext, RunLike, ScheduleLike, VersionLike

PERMISSION_FOR_ACTION = {"task_create": "task.create"}
APPROVAL_STATUSES_RUN = ("APPROVED",)
APPROVAL_STATUSES_SCHEDULE = ("APPROVED", "PARTIALLY_CONSUMED")


@dataclass(frozen=True)
class SelectedAgent:
    agent_id: str
    account_id: str  # public id, used for delegation
    account_uuid: str


@dataclass(frozen=True)
class PolicyResult:
    ok: bool
    skip_code: SkipCode | None = None
    detail: str = ""
    principal: Principal | None = None
    agent: SelectedAgent | None = None
    approval_id: str | None = None
    approval_subject: Subject | None = None
    risk: str = "LOW"


def _denied(code: SkipCode, detail: str) -> PolicyResult:
    return PolicyResult(False, code, detail)


def check(
    ctx: ExecutionContext, run: RunLike, schedule: ScheduleLike, version: VersionLike
) -> PolicyResult:
    now = ctx.now
    if schedule.status != ScheduleStatus.ENABLED.value:
        return _denied(SkipCode.SKIPPED_POLICY, f"schedule is {schedule.status}")
    if version.ends_at is not None and run.scheduled_for > version.ends_at:
        return _denied(SkipCode.SKIPPED_POLICY, "occurrence after ends_at")
    principal = _principal(ctx, version.execution_principal_id)
    if principal is None:
        return _denied(SkipCode.SKIPPED_POLICY, "execution principal inactive")
    template = version.action_template
    action = str(template.get("action", "task_create"))
    permission = PERMISSION_FOR_ACTION.get(action)
    if permission is None:
        return _denied(SkipCode.SKIPPED_POLICY, f"action {action} not executable by schedules")
    if not _member(ctx, version.channel_id, principal.account_uuid):
        return _denied(SkipCode.SKIPPED_POLICY, "execution principal is not a channel member")
    risk, approval_required = _authorize(ctx, principal, permission, version, run)
    if risk is None:
        return _denied(SkipCode.SKIPPED_POLICY, "permission revoked")
    agent = _select_agent(ctx, version, principal)
    if isinstance(agent, PolicyResult):
        return agent
    inp = dict(template.get("input", {}))
    needs_approval = bool(approval_required or inp.get("requires_approval"))
    approval_id: str | None = None
    subject: Subject | None = None
    if needs_approval:
        found = _approval(ctx, run, now)
        if found is None:
            return _denied(SkipCode.SKIPPED_POLICY, "APPROVAL_REQUIRED: no usable Approval")
        approval_id, subject = found
    for ref in template.get("secret_refs", []):
        if agent is None or not _grant_exists(ctx, str(ref), agent.agent_id, now):
            return _denied(SkipCode.SKIPPED_POLICY, f"SECRET_GRANT_MISSING: {ref}")
    return PolicyResult(True, None, "", principal, agent, approval_id, subject, risk)


def _principal(ctx: ExecutionContext, account_uuid: str) -> Principal | None:
    row = ctx.session.execute(
        text("SELECT account_id, account_type, status FROM accounts WHERE id = :i"),
        {"i": uuid.UUID(account_uuid)},
    ).first()
    if row is None or str(row[2]).upper() != "ACTIVE":
        return None
    return Principal(str(row[0]), account_uuid, str(row[1]), f"schedule:{account_uuid[:8]}")


def _member(ctx: ExecutionContext, channel_uuid: str, account_uuid: str) -> bool:
    row = ctx.session.execute(
        text("SELECT 1 FROM channel_members WHERE channel_id = :c AND account_id = :a"),
        {"c": uuid.UUID(channel_uuid), "a": uuid.UUID(account_uuid)},
    ).first()
    return row is not None


def _authorize(
    ctx: ExecutionContext, principal: Principal, permission: str, version: VersionLike, run: RunLike
) -> tuple[str | None, bool]:
    """Risk and approval requirement per the Policy Engine; (None, False) when denied."""
    if ctx.authorizer is None:
        return "LOW", False
    try:
        auth = ctx.authorizer.require(
            ctx.session,
            principal.account_id,
            permission,
            action=f"schedule:{run.schedule_id}",
            channel_id=version.channel_id,
            correlation_id=run.run_id,
        )
    except (bus.CommandError, Exception) as exc:  # AuthorizationDenied or a bus wrapper
        code = getattr(exc, "code", type(exc).__name__)
        if code in ("POLICY_DENIED", "DEFAULT_DENY", "PRINCIPAL_INACTIVE") or "DEN" in code:
            return None, False
        raise
    if auth is None:  # AllowAllAuthorizer (tests)
        return "LOW", False
    if not getattr(auth, "allowed", True):
        return None, False
    return str(getattr(auth, "risk", "LOW")), bool(getattr(auth, "approval_required", False))


def _select_agent(
    ctx: ExecutionContext, version: VersionLike, principal: Principal
) -> SelectedAgent | PolicyResult | None:
    sel = dict(version.agent_selection or {})
    mode = sel.get("mode")
    if not mode:
        return None
    if mode == "fixed":
        agent = _fixed_agent(ctx, str(sel["agent_id"]))
        if agent is not None:
            return agent
        if not sel.get("fallback"):
            return _denied(
                SkipCode.SKIPPED_AGENT_UNAVAILABLE, f"agent {sel['agent_id']} unavailable"
            )
        caps: list[str] = []
        domain = None
    else:
        caps = [str(c) for c in sel.get("required_capabilities", [])]
        domain = sel.get("domain")
    excluded = {str(a) for a in sel.get("exclude_agent_ids", [])}
    found = routing.candidates(
        ctx.session,
        workspace_id=ctx.workspace_id,
        channel_uuid=version.channel_id,
        required_capability=caps[0] if caps else None,
        domain=domain,
        needs_secret_handles=bool(version.action_template.get("secret_refs")),
        authorizer=ctx.authorizer,
        correlation_id=f"schedule:{version.schedule_id}",
    )
    eligible = [c for c in found if c.agent_id not in excluded]
    if not eligible:
        return _denied(
            SkipCode.SKIPPED_AGENT_UNAVAILABLE, "no eligible Agent for the capability query"
        )
    best = sorted(eligible, key=lambda c: (-c.score, c.agent_id))[0]
    return SelectedAgent(best.agent_id, best.account_id, best.account_uuid)


def _fixed_agent(ctx: ExecutionContext, agent_id: str) -> SelectedAgent | None:
    row = ctx.session.execute(
        text(
            "SELECT a.status, a.online, acc.id, acc.account_id, acc.status FROM agents a "
            "JOIN accounts acc ON acc.id = a.account_id WHERE a.agent_id = :g"
        ),
        {"g": agent_id},
    ).first()
    if row is None or str(row[0]) != "active" or not bool(row[1]):
        return None
    if str(row[4]).upper() != "ACTIVE":
        return None
    return SelectedAgent(agent_id, str(row[3]), str(row[2]))


def _approval(ctx: ExecutionContext, run: RunLike, now: dt.datetime) -> tuple[str, Subject] | None:
    """A per-Run Approval first, else a validity/count-limited Schedule Approval."""
    for subject_type, subject_id, statuses in (
        ("run", run.run_id, APPROVAL_STATUSES_RUN),
        ("schedule", run.schedule_id, APPROVAL_STATUSES_SCHEDULE),
    ):
        rows = ctx.session.execute(
            text(
                "SELECT approval_id, max_uses, "
                "(SELECT count(*) FROM approval_consumptions c WHERE "
                "c.approval_id = g.approval_id) "
                "FROM approval_grants g WHERE workspace_id = :w AND subject_type = :t "
                "AND subject_id = :s AND status = ANY(:st) AND valid_from <= :now "
                "AND expires_at > :now ORDER BY expires_at"
            ),
            {
                "w": uuid.UUID(ctx.workspace_id),
                "t": subject_type,
                "s": subject_id,
                "st": list(statuses),
                "now": now,
            },
        ).all()
        for approval_id, max_uses, used in rows:
            if max_uses is not None and int(used) >= int(max_uses):
                continue
            return str(approval_id), Subject(subject_type, subject_id)
    return None


def _grant_exists(ctx: ExecutionContext, secret_ref: str, agent_id: str, now: dt.datetime) -> bool:
    row = ctx.session.execute(
        text(
            "SELECT 1 FROM secret_grants WHERE workspace_id = :w AND secret_ref = :r "
            "AND agent_id = :a AND revoked_at IS NULL AND (expires_at IS "
            "NULL OR expires_at > :now) "
            "AND task_id IS NULL"
        ),
        {"w": uuid.UUID(ctx.workspace_id), "r": secret_ref, "a": agent_id, "now": now},
    ).first()
    return row is not None


def template_value(template: dict[str, Any], key: str, default: Any = None) -> Any:
    return dict(template.get("input", {})).get(key, default)
