"""Server-side Agent Limits enforcement (P3-08; spec §4.2 Limits, development plan §7C).

``enforce_limits`` is called on the bus before an Agent-actor side effect (work poll/result,
Task acceptance, brainstorm turn). Exceeding any configured limit raises the stable error
``AGENT_LIMIT_EXCEEDED`` (HTTP 429) with the limit name, configured value and current value,
and writes an ``agent.limit_exceeded`` audit row in an independent transaction so the audit
survives the rolled-back command. Nothing else changes: no Event, no side effect (V-P3-15).
Limits are integers; an absent key means "unlimited".
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.agents import registry as reg
from server.application.bus import CommandError
from server.domain.clock import Clock
from server.observability.audit import append_audit
from server.usage.records import usage_for

KINDS = ("request", "task_accept", "brainstorm_turn", "task_cost", "task_wall_time")
# Tasks that occupy the Agent: accepted and not yet terminal (a delegated, unaccepted Task is
# exactly what the acceptance check decides on, so it does not count yet)
NON_TERMINAL = ("ACCEPTED", "RUNNING", "WAITING", "IMPLEMENTED", "VERIFYING")


@dataclass(frozen=True)
class LimitCheck:
    limit: str
    configured: int
    current: int

    @property
    def exceeded(self) -> bool:
        return self.current >= self.configured


class AgentLimitExceededError(CommandError):
    def __init__(self, agent_id: str, check: LimitCheck) -> None:
        super().__init__(
            "AGENT_LIMIT_EXCEEDED",
            f"{check.limit} limit {check.configured} reached ({check.current})",
            status=429,
            extra={
                "agent_id": agent_id,
                "limit": check.limit,
                "configured": check.configured,
                "current": check.current,
            },
        )
        self.check = check


def _minute(now: dt.datetime) -> dt.datetime:
    return now.replace(second=0, microsecond=0)


def rate_window_count(session: Session, agent_id: str, now: dt.datetime) -> int:
    row = session.execute(
        text("SELECT requests FROM agent_rate_windows WHERE agent_id = :g AND window_start = :w"),
        {"g": agent_id, "w": _minute(now)},
    ).first()
    return 0 if row is None else int(row[0])


def _count_request(session: Session, agent_id: str, now: dt.datetime) -> None:
    session.execute(
        text(
            "INSERT INTO agent_rate_windows (agent_id, window_start, requests) VALUES (:g, :w, 1) "
            "ON CONFLICT (agent_id, window_start) DO UPDATE "
            "SET requests = agent_rate_windows.requests + 1"
        ),
        {"g": agent_id, "w": _minute(now)},
    )
    session.execute(  # keep the table small: windows older than one hour are useless
        text("DELETE FROM agent_rate_windows WHERE agent_id = :g AND window_start < :old"),
        {"g": agent_id, "old": _minute(now) - dt.timedelta(hours=1)},
    )


def concurrent_tasks(session: Session, account_uuid: uuid.UUID, workspace_id: uuid.UUID) -> int:
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM tasks_projection WHERE assignee_account_id = :a "
                "AND workspace_id = :w AND status = ANY(:st)"
            ),
            {"a": account_uuid, "w": workspace_id, "st": list(NON_TERMINAL)},
        ).scalar_one()
    )


def brainstorm_turns_today(session: Session, agent_id: str, now: dt.datetime) -> int:
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM work_items WHERE agent_id = :g AND kind = 'brainstorm_turn' "
                "AND created_at >= :d AND created_at < :e"
            ),
            {"g": agent_id, "d": day_start, "e": day_start + dt.timedelta(days=1)},
        ).scalar_one()
    )


def task_wall_ms(session: Session, agent_id: str, task_id: str) -> int:
    return int(
        session.execute(
            text(
                "SELECT COALESCE(sum(wall_ms), 0) FROM usage_records "
                "WHERE agent_id = :g AND task_id = :t"
            ),
            {"g": agent_id, "t": task_id},
        ).scalar_one()
    )


def _audit_independent(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    agent_id: str,
    actor_account_uuid: uuid.UUID | None,
    actor_label: str,
    correlation_id: str,
    check: LimitCheck,
    kind: str,
    clock: Clock,
) -> None:
    bind = session.get_bind()
    with Session(bind) as own, own.begin():
        append_audit(
            own,
            action="agent.limit_exceeded",
            target_type="agent",
            target_id=agent_id,
            result="DENY",
            actor_label=actor_label,
            correlation_id=correlation_id,
            workspace_id=workspace_id,
            actor_account_id=actor_account_uuid,
            error_code="AGENT_LIMIT_EXCEEDED",
            metadata={
                "limit": check.limit,
                "configured": check.configured,
                "current": check.current,
                "kind": kind,
            },
            clock=clock,
        )


def evaluate(
    session: Session,
    row: reg.AgentRow,
    kind: str,
    now: dt.datetime,
    *,
    task_id: str | None = None,
    exclude_task_id: str | None = None,
) -> list[LimitCheck]:
    """Every configured limit relevant to ``kind`` with its current value (no side effects)."""
    if kind not in KINDS:
        raise CommandError("AGENT_LIMIT_KIND_INVALID", kind, status=400)
    limits = row.limits
    checks: list[LimitCheck] = []
    rpm = limits.get("requests_per_minute")
    if rpm is not None:
        checks.append(
            LimitCheck("requests_per_minute", rpm, rate_window_count(session, row.agent_id, now))
        )
    if kind == "task_accept" and (cap := limits.get("concurrent_tasks")) is not None:
        current = concurrent_tasks(session, row.account_id, row.workspace_id)
        if exclude_task_id is not None:
            assigned = session.execute(
                text(
                    "SELECT 1 FROM tasks_projection WHERE task_id = :t AND "
                    "assignee_account_id = :a "
                    "AND status = ANY(:st)"
                ),
                {"t": exclude_task_id, "a": row.account_id, "st": list(NON_TERMINAL)},
            ).first()
            if assigned is not None:
                current -= 1  # the Task being accepted already counts as assigned
        checks.append(LimitCheck("concurrent_tasks", cap, current))
    if kind == "brainstorm_turn" and (turns := limits.get("brainstorm_turns")) is not None:
        checks.append(
            LimitCheck(
                "brainstorm_turns", turns, brainstorm_turns_today(session, row.agent_id, now)
            )
        )
    if kind in ("task_cost", "request", "task_accept"):
        if (daily := limits.get("daily_cost_units")) is not None:
            checks.append(
                LimitCheck(
                    "daily_cost_units",
                    daily,
                    usage_for(session, "agent_daily", row.agent_id, now.date()),
                )
            )
        if task_id and (per_task := limits.get("per_task_cost_units")) is not None:
            checks.append(
                LimitCheck(
                    "per_task_cost_units",
                    per_task,
                    usage_for(session, "agent_task", f"{row.agent_id}:{task_id}"),
                )
            )
    if (
        kind == "task_wall_time"
        and task_id
        and (wall := limits.get("per_task_wall_ms")) is not None
    ):
        checks.append(
            LimitCheck("per_task_wall_ms", wall, task_wall_ms(session, row.agent_id, task_id))
        )
    return checks


def _reroute_on_budget(
    session: Session, task_id: str, workspace_id: uuid.UUID, clock: Clock
) -> None:
    """§7C/§7D.3: a budget overrun re-routes the Task once (or WAITING) in its own transaction,
    because the offending command is about to be rejected and rolled back."""
    from server.agents import rerouting
    from server.events.postgres_store import PostgresEventStore

    bind = session.get_bind()
    with Session(bind) as own, own.begin():
        try:
            rerouting.on_budget_exceeded(
                own,
                PostgresEventStore(own, clock=clock),
                task_id,
                clock=clock,
                workspace_id=str(workspace_id),
            )
        except CommandError as exc:  # no system Account / nothing assigned: nothing to re-route
            if exc.code not in ("SYSTEM_ACCOUNT_MISSING", "TASK_NOT_FOUND"):
                raise


def enforce_limits(
    session: Session,
    agent_id: str,
    kind: str,
    clock: Clock,
    *,
    workspace_id: str | uuid.UUID | None = None,
    task_id: str | None = None,
    exclude_task_id: str | None = None,
    actor_account_uuid: str | uuid.UUID | None = None,
    actor_label: str = "-",
    correlation_id: str = "-",
    count_request: bool = True,
) -> list[LimitCheck]:
    """Raise ``AGENT_LIMIT_EXCEEDED`` (audited) or record the request and return the checks."""
    now = clock.now()
    ws = uuid.UUID(str(workspace_id)) if workspace_id else None
    row = _agent(session, agent_id, ws)
    checks = evaluate(session, row, kind, now, task_id=task_id, exclude_task_id=exclude_task_id)
    for check in checks:
        if check.exceeded:
            _audit_independent(
                session,
                workspace_id=row.workspace_id,
                agent_id=agent_id,
                actor_account_uuid=None
                if actor_account_uuid is None
                else uuid.UUID(str(actor_account_uuid)),
                actor_label=actor_label,
                correlation_id=correlation_id,
                check=check,
                kind=kind,
                clock=clock,
            )
            if task_id and check.limit in ("daily_cost_units", "per_task_cost_units"):
                _reroute_on_budget(session, task_id, row.workspace_id, clock)
            raise AgentLimitExceededError(agent_id, check)
    if count_request and row.limits.get("requests_per_minute") is not None:
        _count_request(session, agent_id, now)
    return checks


def _agent(session: Session, agent_id: str, workspace_id: uuid.UUID | None) -> reg.AgentRow:
    if workspace_id is not None:
        row = reg.load_agent(session, workspace_id, agent_id)
    else:
        found = session.execute(
            text("SELECT workspace_id FROM agents WHERE agent_id = :g"), {"g": agent_id}
        ).first()
        row = None if found is None else reg.load_agent(session, found[0], agent_id)
    if row is None:
        raise CommandError("AGENT_NOT_FOUND", agent_id, status=404)
    return row


def limits_view(session: Session, row: reg.AgentRow, now: dt.datetime) -> dict[str, Any]:
    """Configured limits with their current counters (Admin UI / API read)."""
    current: dict[str, int] = {}
    for kind in ("request", "task_accept", "brainstorm_turn"):
        for check in evaluate(session, row, kind, now):
            current[check.limit] = check.current
    return {"limits": dict(row.limits), "current": current}
