"""Usage records (development plan §7C): every work result, invoke result, and heartbeat reports
usage; the server computes ``cost_units`` from the current pricing version and stores an
append-only ``usage_records`` row. Missing usage without a ``usage_unavailable`` reason is
rejected before anything is written (V-P1-30)."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.clock import Clock, SystemClock
from server.usage.pricing import Pricing, UsageError, compute_cost_units
from server.usage.versions import current_pricing

SCOPE_TYPES = ("agent_daily", "agent_task", "channel_daily", "schedule_run", "schedule_daily")


@dataclass(frozen=True)
class UsageRecord:
    record_id: int
    cost_units: int
    source: str
    pricing_version: str
    model: str | None
    unavailable_reason: str | None


def build_report(
    usage: dict[str, Any] | None, usage_unavailable_reason: str | None
) -> dict[str, Any]:
    """Assemble the §7C report; ``USAGE_REQUIRED`` when neither usage nor a reason is present."""
    if usage is not None:
        return {"usage": usage}
    if usage_unavailable_reason is not None:
        return {"usage_unavailable": {"reason": usage_unavailable_reason}}
    raise UsageError("USAGE_REQUIRED", "usage or usage_unavailable reason required")


def record_usage(
    session: Session,
    *,
    workspace_id: str,
    account_id: str,
    agent_id: str | None,
    work_item_id: str | None,
    usage: dict[str, Any] | None,
    usage_unavailable_reason: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
    brainstorm_id: str | None = None,
    document_id: str | None = None,
    clock: Clock | None = None,
    pricing: Pricing | None = None,
) -> UsageRecord:
    report = build_report(usage, usage_unavailable_reason)
    pricing = pricing or current_pricing(session)
    cost = compute_cost_units(report, pricing)
    now = (clock or SystemClock()).now()
    u = report.get("usage") or {}
    params: dict[str, Any] = {
        "ws": uuid.UUID(workspace_id),
        "agent": agent_id,
        "acct": uuid.UUID(account_id),
        "task": task_id,
        "run": run_id,
        "bs": brainstorm_id,
        "doc": document_id,
        "wi": work_item_id,
        "model": cost.model if cost else None,
        "tin": int(u.get("input_tokens", 0)),
        "tout": int(u.get("output_tokens", 0)),
        "calls": int(u.get("tool_calls", 0)),
        "wall": int(u.get("wall_time_ms", 0)),
        "cost": cost.cost_units if cost else 0,
        "source": cost.source if cost else "unavailable",
        "reason": None if cost else report["usage_unavailable"]["reason"],
        "pv": pricing.version,
        "at": now,
    }
    record_id = session.execute(
        text(
            "INSERT INTO usage_records (workspace_id, agent_id, account_id, task_id, run_id, "
            "brainstorm_id, document_id, work_item_id, model, input_tokens, output_tokens, "
            "tool_calls, wall_ms, cost_units, source, unavailable_reason, pricing_version, "
            "reported_at) VALUES (:ws, :agent, :acct, :task, :run, :bs, :doc, :wi, :model, :tin, "
            ":tout, :calls, :wall, :cost, :source, :reason, :pv, :at) RETURNING id"
        ),
        params,
    ).scalar_one()
    return UsageRecord(
        int(record_id),
        params["cost"],
        params["source"],
        pricing.version,
        params["model"],
        params["reason"],
    )


def _day_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC)
    return start, start + dt.timedelta(days=1)


def usage_for(
    session: Session,
    scope_type: str,
    scope_id: str,
    day: dt.date | None = None,
    *,
    run_ids: list[str] | None = None,
) -> int:
    """Sum of ``cost_units`` for a budget scope (UTC day for the daily scopes).

    Scope ids: ``agent_daily`` = agent_id; ``agent_task`` = ``<agent_id>|<task_id>``;
    ``channel_daily`` = channel uuid (joined through tasks_projection); ``schedule_run`` = run_id;
    ``schedule_daily`` = schedule_id, resolved through ``run_ids`` supplied by the Schedule
    service (Phase 5) — without them the scope sums nothing.
    """
    if scope_type not in SCOPE_TYPES:
        raise UsageError("BUDGET_SCOPE_INVALID", scope_type)
    params: dict[str, Any] = {}
    where: list[str] = []
    if scope_type in ("agent_daily", "agent_task", "channel_daily", "schedule_daily"):
        if day is None:
            raise UsageError("BUDGET_SCOPE_INVALID", f"{scope_type} needs a day")
        start, end = _day_bounds(day)
        where.append("u.reported_at >= :start AND u.reported_at < :end")
        params.update({"start": start, "end": end})
    join = ""
    if scope_type == "agent_daily":
        where.append("u.agent_id = :sid")
        params["sid"] = scope_id
    elif scope_type == "agent_task":
        agent_id, _, task_id = scope_id.partition("|")
        where.append("u.agent_id = :aid AND u.task_id = :tid")
        params.update({"aid": agent_id, "tid": task_id})
    elif scope_type == "channel_daily":
        join = "JOIN tasks_projection t ON t.task_id = u.task_id"
        where.append("t.channel_id = :cid")
        params["cid"] = uuid.UUID(scope_id)
    elif scope_type == "schedule_run":
        where.append("u.run_id = :rid")
        params["rid"] = scope_id
    else:  # schedule_daily
        if not run_ids:
            return 0
        where.append("u.run_id = ANY(:rids)")
        params["rids"] = list(run_ids)
    clause = " AND ".join(where)
    sql = f"SELECT COALESCE(SUM(u.cost_units), 0) FROM usage_records u {join} WHERE {clause}"  # noqa: S608
    return int(session.execute(text(sql), params).scalar_one())


def estimate_for(session: Session, agent_id: str, kind: str, default: int, window: int = 20) -> int:
    """Recent average cost (ceil) of the same Agent and work-item kind, else ``default``."""
    rows = (
        session.execute(
            text(
                "SELECT u.cost_units FROM usage_records u "
                "JOIN work_items w ON w.work_item_id = u.work_item_id "
                "WHERE u.agent_id = :a AND w.kind = :k AND u.source <> 'unavailable' "
                "ORDER BY u.reported_at DESC, u.id DESC LIMIT :n"
            ),
            {"a": agent_id, "k": kind, "n": window},
        )
        .scalars()
        .all()
    )
    if not rows:
        return default
    total = sum(int(r) for r in rows)
    return -(-total // len(rows))
