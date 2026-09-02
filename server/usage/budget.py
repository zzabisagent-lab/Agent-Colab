"""Budget reservation and settlement (development plan §7C).

Before a work item is delivered, an estimate is reserved against the scope's limit inside the
same transaction under a per-scope advisory lock. ``used_today + reserved + estimate > limit``
means no reservation: a ``BUDGET_EXCEEDED`` Event is appended and the caller must not perform
the side effect (the Task goes to ``WAITING``). After the result arrives the reservation is
settled with the actual cost; an overrun marks it ``exceeded`` and ``assert_not_overrun`` blocks
the next side effect in that scope. All values are integer ``cost_units``; days are UTC per the
injected ``Clock``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.application.bus import CommandError
from server.domain.clock import Clock, SystemClock
from server.events.store import AppendRequest, EventStore
from server.usage.records import SCOPE_TYPES, usage_for

# Policy default estimates per work-item kind when an Agent has no usage history (§7C
# "recent average for the same Agent and kind, else the policy default"), in cost_units.
DEFAULT_ESTIMATES: dict[str, int] = {
    "task_assignment": 50_000,
    "subtask_assignment": 50_000,
    "invoke": 20_000,
    "cancel": 1_000,
    "brainstorm_turn": 10_000,
    "verification_assignment": 30_000,
}


@dataclass(frozen=True)
class BudgetScope:
    scope_type: str
    scope_id: str

    def aggregate_id(self) -> str:
        return f"bud-{self.scope_type}:{self.scope_id}"[:200]


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    scope: BudgetScope
    estimated_cost_units: int
    event_id: str


@dataclass(frozen=True)
class ReservationOutcome:
    """Result of ``try_reserve``: either a reservation or the exceeded details (with the Event)."""

    reserved: bool
    reservation: Reservation | None
    event_id: str
    limit_cost_units: int
    requested_cost_units: int
    used_cost_units: int
    reserved_cost_units: int


class BudgetExceededError(CommandError):
    def __init__(self, outcome: ReservationOutcome) -> None:
        super().__init__(
            "BUDGET_EXCEEDED",
            f"{outcome.requested_cost_units} cost_units requested, "
            f"limit {outcome.limit_cost_units}",
            status=409,
            extra={
                "limit_cost_units": outcome.limit_cost_units,
                "requested_cost_units": outcome.requested_cost_units,
                "used_cost_units": outcome.used_cost_units,
                "reserved_cost_units": outcome.reserved_cost_units,
                "event_id": outcome.event_id,
            },
        )
        self.outcome = outcome


def would_exceed(used: int, reserved: int, estimate: int, limit: int) -> bool:
    """Pure decision: the reservation is refused when it would push the scope over the limit."""
    return used + reserved + estimate > limit


def settlement_status(actual: int, available: int) -> str:
    return "exceeded" if actual > available else "settled"


def _lock(session: Session, scope: BudgetScope) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": f"budget:{scope.scope_type}:{scope.scope_id}"},
    )


def _day_window(clock: Clock) -> tuple[dt.datetime, dt.datetime, dt.date]:
    now = clock.now()
    day = now.date()
    start = dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC)
    return start, start + dt.timedelta(days=1), day


def reserved_cost_units(
    session: Session, scope: BudgetScope, clock: Clock, exclude_reservation_id: str | None = None
) -> int:
    start, end, _ = _day_window(clock)
    row = session.execute(
        text(
            "SELECT COALESCE(SUM(estimated_cost_units), 0) FROM budget_reservations "
            "WHERE scope_type = :t AND scope_id = :s AND status = 'reserved' "
            "AND created_at >= :start AND created_at < :end "
            "AND (CAST(:ex AS text) IS NULL OR reservation_id <> CAST(:ex AS text))"
        ),
        {
            "t": scope.scope_type,
            "s": scope.scope_id,
            "start": start,
            "end": end,
            "ex": exclude_reservation_id,
        },
    ).scalar_one()
    return int(row)


def used_cost_units(
    session: Session, scope: BudgetScope, clock: Clock, run_ids: list[str] | None = None
) -> int:
    _, _, day = _day_window(clock)
    daily = scope.scope_type in ("agent_daily", "agent_task", "channel_daily", "schedule_daily")
    return usage_for(
        session, scope.scope_type, scope.scope_id, day if daily else None, run_ids=run_ids
    )


def try_reserve(
    session: Session,
    store: EventStore,
    *,
    workspace_id: str,
    actor_account_id: str,
    scope: BudgetScope,
    limit_cost_units: int,
    estimate: int,
    work_item_id: str | None,
    correlation_id: str,
    idempotency_key: str | None = None,
    clock: Clock | None = None,
    run_ids: list[str] | None = None,
) -> ReservationOutcome:
    """Reserve ``estimate`` against the scope; never raises on exceeded (see ``reserve``)."""
    if scope.scope_type not in SCOPE_TYPES:
        raise CommandError("BUDGET_SCOPE_INVALID", scope.scope_type, status=400)
    if estimate < 0 or limit_cost_units < 0:
        raise CommandError("BUDGET_VALUE_INVALID", "cost_units must be non-negative", status=400)
    clock = clock or SystemClock()
    _lock(session, scope)
    used = used_cost_units(session, scope, clock, run_ids)
    reserved = reserved_cost_units(session, scope, clock)
    key = idempotency_key or f"{work_item_id or uuid.uuid4().hex}:{estimate}"
    if would_exceed(used, reserved, estimate, limit_cost_units):
        result = store.append(
            AppendRequest(
                workspace_id=workspace_id,
                aggregate_type="budget",
                aggregate_id=scope.aggregate_id(),
                type="BUDGET_EXCEEDED",
                actor_account_id=actor_account_id,
                correlation_id=correlation_id,
                idempotency_scope="budget:exceeded",
                idempotency_key=key,
                payload={
                    "scope_type": scope.scope_type,
                    "scope_id": scope.scope_id,
                    "limit_cost_units": limit_cost_units,
                    "requested_cost_units": estimate,
                    "used_cost_units": used,
                    "reserved_cost_units": reserved,
                    "work_item_id": work_item_id,
                },
            )
        )
        return ReservationOutcome(
            False, None, result.event_id, limit_cost_units, estimate, used, reserved
        )
    reservation_id = "bres-" + uuid.uuid4().hex[:20]
    session.execute(
        text(
            "INSERT INTO budget_reservations (id, reservation_id, workspace_id, scope_type, "
            "scope_id, work_item_id, estimated_cost_units, status, created_at) VALUES "
            "(:id, :rid, :ws, :t, :s, :wi, :est, 'reserved', :at)"
        ),
        {
            "id": uuid.uuid4(),
            "rid": reservation_id,
            "ws": uuid.UUID(workspace_id),
            "t": scope.scope_type,
            "s": scope.scope_id,
            "wi": work_item_id,
            "est": estimate,
            "at": clock.now(),
        },
    )
    result = store.append(
        AppendRequest(
            workspace_id=workspace_id,
            aggregate_type="budget",
            aggregate_id=scope.aggregate_id(),
            type="BUDGET_RESERVED",
            actor_account_id=actor_account_id,
            correlation_id=correlation_id,
            idempotency_scope="budget:reserve",
            idempotency_key=reservation_id,
            payload={
                "reservation_id": reservation_id,
                "scope_type": scope.scope_type,
                "scope_id": scope.scope_id,
                "cost_units": estimate,
                "work_item_id": work_item_id,
            },
        )
    )
    reservation = Reservation(reservation_id, scope, estimate, result.event_id)
    return ReservationOutcome(
        True, reservation, result.event_id, limit_cost_units, estimate, used, reserved
    )


def reserve(session: Session, store: EventStore, **kwargs: Any) -> Reservation:
    """Like ``try_reserve`` but raises ``BudgetExceededError``. The caller must
    still commit the transaction so that the ``BUDGET_EXCEEDED`` Event persists."""
    outcome = try_reserve(session, store, **kwargs)
    if not outcome.reserved or outcome.reservation is None:
        raise BudgetExceededError(outcome)
    return outcome.reservation


def _load_reservation(session: Session, reservation_id: str) -> dict[str, Any]:
    row = (
        session.execute(
            text(
                "SELECT reservation_id, scope_type, scope_id, estimated_cost_units, status "
                "FROM budget_reservations WHERE reservation_id = :r FOR UPDATE"
            ),
            {"r": reservation_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise CommandError("BUDGET_RESERVATION_UNKNOWN", reservation_id, status=404)
    return dict(row)


def settle(
    session: Session,
    reservation_id: str,
    actual_cost_units: int,
    limit_cost_units: int,
    clock: Clock | None = None,
    run_ids: list[str] | None = None,
) -> str:
    """Settle with the actual cost; returns ``settled`` or ``exceeded`` (overrun recorded)."""
    if actual_cost_units < 0:
        raise CommandError("BUDGET_VALUE_INVALID", "cost_units must be non-negative", status=400)
    clock = clock or SystemClock()
    row = _load_reservation(session, reservation_id)
    scope = BudgetScope(row["scope_type"], row["scope_id"])
    _lock(session, scope)
    if row["status"] != "reserved":
        raise CommandError("BUDGET_RESERVATION_NOT_OPEN", f"{reservation_id} is {row['status']}")
    used_others = used_cost_units(session, scope, clock, run_ids)
    reserved_others = reserved_cost_units(
        session, scope, clock, exclude_reservation_id=reservation_id
    )
    available = limit_cost_units - used_others - reserved_others
    status = settlement_status(actual_cost_units, available)
    session.execute(
        text(
            "UPDATE budget_reservations SET status = :st, settled_cost_units = :a, "
            "settled_at = :at WHERE reservation_id = :r"
        ),
        {"st": status, "a": actual_cost_units, "at": clock.now(), "r": reservation_id},
    )
    return status


def release(session: Session, reservation_id: str, clock: Clock | None = None) -> None:
    row = _load_reservation(session, reservation_id)
    if row["status"] != "reserved":
        raise CommandError("BUDGET_RESERVATION_NOT_OPEN", f"{reservation_id} is {row['status']}")
    session.execute(
        text(
            "UPDATE budget_reservations SET status = 'released', settled_at = :at "
            "WHERE reservation_id = :r"
        ),
        {"at": (clock or SystemClock()).now(), "r": reservation_id},
    )


def assert_not_overrun(session: Session, scope: BudgetScope, clock: Clock | None = None) -> None:
    """Block the next side effect while an overrun (``exceeded`` settlement) exists for the day."""
    start, end, _ = _day_window(clock or SystemClock())
    n = session.execute(
        text(
            "SELECT count(*) FROM budget_reservations WHERE scope_type = :t AND scope_id = :s "
            "AND status = 'exceeded' AND settled_at >= :start AND settled_at < :end"
        ),
        {"t": scope.scope_type, "s": scope.scope_id, "start": start, "end": end},
    ).scalar_one()
    if int(n) > 0:
        raise CommandError(
            "BUDGET_EXCEEDED", f"{scope.scope_type} {scope.scope_id} overran its budget", status=409
        )
