"""Per-Run and daily cost_units budgets for Schedule Runs (development plan §7C, §10A.1; P5-10).

Before a Run's side effect an estimate is reserved against the ``schedule_run`` (per-Run) and
``schedule_daily`` scopes through the Phase 1 budget module; the reservation is settled from
``usage_records`` when the Run ends. An overrun raises a ``budget_alerts`` row, a notification, and
blocks the next Run of the Schedule with ``BUDGET_EXCEEDED``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from server.application.bus import CommandError
from server.notifications import outbox as notification_outbox
from server.usage import budget as core_budget
from server.usage.records import usage_for

if TYPE_CHECKING:
    from server.schedules.execution import ExecutionContext, RunLike, VersionLike

log = logging.getLogger(__name__)
DEFAULT_ESTIMATE = 10


@dataclass(frozen=True)
class ReserveResult:
    ok: bool
    detail: str = ""
    reservations: tuple[str, ...] = ()


def policy_of(version: VersionLike) -> dict[str, Any]:
    return dict(version.budget_policy or {})


def reserve_for_run(ctx: ExecutionContext, run: RunLike, version: VersionLike) -> ReserveResult:
    policy = policy_of(version)
    estimate = int(policy.get("estimate_cost_units", DEFAULT_ESTIMATE))
    per_run = policy.get("per_run_cost_units")
    daily = policy.get("daily_cost_units")
    if per_run is None and daily is None:
        return ReserveResult(True)
    day = ctx.now.date()
    run_ids = ctx.store.run_ids_for_day(ctx.session, run.schedule_id, day)
    if run.run_id not in run_ids:
        run_ids.append(run.run_id)
    reservations: list[str] = []
    for scope_type, limit, ids in (
        ("schedule_run", per_run, [run.run_id]),
        ("schedule_daily", daily, run_ids),
    ):
        if limit is None:
            continue
        scope = core_budget.BudgetScope(
            scope_type, run.run_id if scope_type == "schedule_run" else run.schedule_id
        )
        try:
            core_budget.assert_not_overrun(ctx.session, scope, ctx.clock)
        except CommandError as exc:
            _alert(ctx, run, "budget_exceeded", {"scope": scope_type, "reason": "overrun_open"})
            return ReserveResult(False, exc.detail, tuple(reservations))
        outcome = core_budget.try_reserve(
            ctx.session,
            ctx.event_store,
            workspace_id=ctx.workspace_id,
            actor_account_id=ctx.actor.account_uuid,
            scope=scope,
            limit_cost_units=int(limit),
            estimate=estimate,
            work_item_id=None,
            correlation_id=run.run_id,
            idempotency_key=f"{run.run_id}:budget:{scope_type}",
            clock=ctx.clock,
            run_ids=ids,
        )
        if not outcome.reserved or outcome.reservation is None:
            _alert(
                ctx,
                run,
                "budget_exceeded",
                {
                    "scope": scope_type,
                    "limit": outcome.limit_cost_units,
                    "requested": outcome.requested_cost_units,
                    "used": outcome.used_cost_units,
                    "reserved": outcome.reserved_cost_units,
                },
            )
            return ReserveResult(
                False,
                f"{scope_type}: {outcome.used_cost_units}+{outcome.reserved_cost_units}+"
                f"{outcome.requested_cost_units} > {outcome.limit_cost_units}",
                tuple(reservations),
            )
        ctx.session.execute(
            text(
                "INSERT INTO schedule_run_budgets (run_id, scope_type, reservation_id, "
                "limit_cost_units, estimate_cost_units) VALUES (:r, :t, :res, :l, :e) "
                "ON CONFLICT (run_id, scope_type) DO NOTHING"
            ),
            {
                "r": run.run_id,
                "t": scope_type,
                "res": outcome.reservation.reservation_id,
                "l": int(limit),
                "e": estimate,
            },
        )
        reservations.append(outcome.reservation.reservation_id)
    return ReserveResult(True, "", tuple(reservations))


def _open_reservations(ctx: ExecutionContext, run_id: str) -> list[tuple[str, str, int]]:
    rows = ctx.session.execute(
        text(
            "SELECT scope_type, reservation_id, limit_cost_units FROM schedule_run_budgets "
            "WHERE run_id = :r AND status = 'reserved'"
        ),
        {"r": run_id},
    ).all()
    return [(str(r[0]), str(r[1]), int(r[2])) for r in rows]


def release_for_run(ctx: ExecutionContext, run: RunLike) -> None:
    """A Run that never started its side effect gives its reservation back."""
    for scope_type, reservation_id, _limit in _open_reservations(ctx, run.run_id):
        try:
            core_budget.release(ctx.session, reservation_id, ctx.clock)
        except CommandError:
            continue
        ctx.session.execute(
            text(
                "UPDATE schedule_run_budgets SET status = 'released', settled_at = :now "
                "WHERE run_id = :r AND scope_type = :t"
            ),
            {"now": ctx.now, "r": run.run_id, "t": scope_type},
        )


def settle_for_run(ctx: ExecutionContext, run: RunLike) -> dict[str, str]:
    """Settle the Run's reservations from the usage actually reported for it (§7C)."""
    actual = int(usage_for(ctx.session, "schedule_run", run.run_id))
    statuses: dict[str, str] = {}
    day = ctx.now.date()
    for scope_type, reservation_id, limit in _open_reservations(ctx, run.run_id):
        run_ids = (
            [run.run_id]
            if scope_type == "schedule_run"
            else ctx.store.run_ids_for_day(ctx.session, run.schedule_id, day)
        )
        status = core_budget.settle(ctx.session, reservation_id, actual, limit, ctx.clock, run_ids)
        statuses[scope_type] = status
        ctx.session.execute(
            text(
                "UPDATE schedule_run_budgets SET status = :s, settled_cost_units = :a, "
                "settled_at = :now WHERE run_id = :r AND scope_type = :t"
            ),
            {"s": status, "a": actual, "now": ctx.now, "r": run.run_id, "t": scope_type},
        )
        if status == "exceeded":
            _alert(
                ctx,
                run,
                "budget_exceeded",
                {"scope": scope_type, "actual": actual, "limit": limit, "stage": "settlement"},
            )
    return statuses


def run_usage(ctx: ExecutionContext, run_id: str) -> int:
    return int(usage_for(ctx.session, "schedule_run", run_id))


def _alert(ctx: ExecutionContext, run: RunLike, kind: str, detail: dict[str, Any]) -> None:
    raise_alert(
        ctx.session,
        workspace_id=ctx.workspace_id,
        kind=kind,
        schedule_id=run.schedule_id,
        run_id=run.run_id,
        detail=detail,
        now=ctx.now,
    )


def raise_alert(
    session: Any,
    *,
    workspace_id: str,
    kind: str,
    schedule_id: str | None,
    run_id: str | None,
    detail: dict[str, Any],
    now: dt.datetime,
) -> int:
    """Record an alert row and queue an administrator notification (§7G rules apply on drain)."""
    row = session.execute(
        text(
            "INSERT INTO budget_alerts (workspace_id, kind, schedule_id, "
            "run_id, detail, raised_at, "
            "notified) VALUES (:w, :k, :s, :r, CAST(:d AS jsonb), :now, true) RETURNING id"
        ),
        {
            "w": uuid.UUID(workspace_id),
            "k": kind,
            "s": schedule_id,
            "r": run_id,
            "d": json.dumps(detail, default=str),
            "now": now,
        },
    ).first()
    alert_id = int(row[0]) if row else 0
    notification_outbox.enqueue(
        session,
        workspace_id,
        "notification",
        "mattermost:ops",
        f"schedule-alert:{kind}:{run_id or schedule_id}:{alert_id}",
        {
            "event_type": "SCHEDULE_ALERT",
            "kind": kind,
            "schedule_id": schedule_id,
            "run_id": run_id,
            "detail": detail,
        },
        None,
        now,
    )
    return alert_id


def alerts(session: Any, workspace_id: str, kind: str | None = None) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            "SELECT id, kind, schedule_id, run_id, detail, raised_at FROM budget_alerts "
            "WHERE workspace_id = :w AND (CAST(:k AS text) IS NULL OR kind = CAST(:k AS text)) "
            "ORDER BY id"
        ),
        {"w": uuid.UUID(workspace_id), "k": kind},
    ).all()
    return [
        {
            "id": int(r[0]),
            "kind": str(r[1]),
            "schedule_id": r[2],
            "run_id": r[3],
            "detail": r[4] if isinstance(r[4], dict) else json.loads(r[4] or "{}"),
            "raised_at": r[5],
        }
        for r in rows
    ]
