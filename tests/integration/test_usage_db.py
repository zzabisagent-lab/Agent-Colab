"""P1-14 DB tests: pricing activation, usage records via the real store, aggregation, concurrent
reservations, settlement overrun, BUDGET_EXCEEDED Events (V-P1-30)."""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application.bus import CommandContext, CommandError, Principal, execute
from server.application.usage import ReportUsage, usage_summary
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore
from server.usage.budget import (
    BudgetExceededError,
    BudgetScope,
    Reservation,
    assert_not_overrun,
    release,
    reserve,
    settle,
    try_reserve,
)
from server.usage.pricing import UsageError
from server.usage.records import estimate_for, record_usage, usage_for
from server.usage.versions import activate_from_file, activate_pricing, current_pricing

pytestmark = pytest.mark.db

WS = uuid.uuid4()
ACTOR = uuid.uuid4()
CHANNEL = uuid.uuid4()
AGENT = "agent-usage-1"
CLOCK = FixedClock(dt.datetime(2026, 3, 1, 12, 0, tzinfo=dt.UTC))
DAY = dt.date(2026, 3, 1)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-usage', 'usage')"
            ),
            {"i": WS},
        )
        c.execute(
            text(
                "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
                "VALUES (:i, 'acct-usage', :w, 'agent', 'u')"
            ),
            {"i": ACTOR, "w": WS},
        )
        c.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name) "
                "VALUES (:i, 'chan-usage', :w, 'work', 'u')"
            ),
            {"i": CHANNEL, "w": WS},
        )
        c.execute(
            text(
                "INSERT INTO tasks_projection (task_id, workspace_id, root_task_id, channel_id, "
                "title, domain, risk, status, created_at, updated_at) VALUES ('task-usage-1', :w, "
                "'task-usage-1', :c, 't', 'd', 'LOW', 'RUNNING', now(), now())"
            ),
            {"w": WS, "c": CHANNEL},
        )
        c.execute(
            text(
                "INSERT INTO work_items (work_item_id, workspace_id, kind, agent_id, task_id, "
                "correlation_id, deadline, payload, expected_result_schema, idempotency_key, "
                "status, created_at, updated_at) VALUES ('wi-usage-1', :w, 'task_assignment', :a, "
                "'task-usage-1', 'corr', now(), '{}'::jsonb, 'work-result.v1', 'wi-usage-1', "
                "'ACKED', now(), now())"
            ),
            {"w": WS, "a": AGENT},
        )
    with Session(eng) as s, s.begin():
        activate_from_file(s, activated_by=str(ACTOR))
    yield eng
    eng.dispose()


def _store(s: Session) -> PostgresEventStore:
    return PostgresEventStore(s, clock=CLOCK)


def _usage(model: str, **over: int) -> dict[str, int | str]:
    base: dict[str, int | str] = {
        "model": model,
        "input_tokens": 1234,
        "output_tokens": 567,
        "tool_calls": 3,
        "wall_time_ms": 4500,
    }
    base.update(over)
    return base


def test_pricing_activation_is_idempotent_and_immutable(engine: Engine) -> None:
    with Session(engine) as s, s.begin():
        assert activate_from_file(s) == "pricing-v1"  # identical content, no error
        assert current_pricing(s).version == "pricing-v1"
        table = {
            "version": "pricing-v1",
            "cost_units_per_credit": 1000000,
            "default": {
                "input_per_1k_tokens": 1,
                "output_per_1k_tokens": 1,
                "per_tool_call": 1,
                "per_wall_second": 1,
            },
            "models": {},
        }
        with pytest.raises(UsageError) as exc:
            activate_pricing(s, table)
        assert exc.value.code == "PRICING_VERSION_IMMUTABLE"
        assert s.execute(text("SELECT count(*) FROM pricing_versions")).scalar_one() == 1


def test_record_usage_known_unknown_reported_and_missing(engine: Engine) -> None:
    with Session(engine) as s, s.begin():
        known = record_usage(
            s,
            workspace_id=str(WS),
            account_id=str(ACTOR),
            agent_id=AGENT,
            work_item_id="wi-usage-1",
            usage=_usage("generic-small"),
            task_id="task-usage-1",
            clock=CLOCK,
        )
        unknown = record_usage(
            s,
            workspace_id=str(WS),
            account_id=str(ACTOR),
            agent_id=AGENT,
            work_item_id="wi-usage-1",
            usage=_usage("mystery-9000"),
            task_id="task-usage-1",
            clock=CLOCK,
        )
        reported = record_usage(
            s,
            workspace_id=str(WS),
            account_id=str(ACTOR),
            agent_id=AGENT,
            work_item_id="wi-usage-1",
            usage=_usage("generic-small", cost_units=777),
            task_id="task-usage-1",
            clock=CLOCK,
        )
        unavailable = record_usage(
            s,
            workspace_id=str(WS),
            account_id=str(ACTOR),
            agent_id=AGENT,
            work_item_id="wi-usage-1",
            usage=None,
            usage_unavailable_reason="ADAPTER_NO_METERING",
            task_id="task-usage-1",
            clock=CLOCK,
        )
        assert (known.cost_units, known.source) == (850, "computed")
        assert (unknown.cost_units, unknown.source) == (13752, "estimated")
        assert (reported.cost_units, reported.source) == (777, "reported")
        assert (unavailable.cost_units, unavailable.source, unavailable.unavailable_reason) == (
            0,
            "unavailable",
            "ADAPTER_NO_METERING",
        )
        before = s.execute(text("SELECT count(*) FROM usage_records")).scalar_one()
        with pytest.raises(UsageError) as exc:
            record_usage(
                s,
                workspace_id=str(WS),
                account_id=str(ACTOR),
                agent_id=AGENT,
                work_item_id="wi-usage-1",
                usage=None,
                clock=CLOCK,
            )
        assert exc.value.code == "USAGE_REQUIRED"
        assert s.execute(text("SELECT count(*) FROM usage_records")).scalar_one() == before
        rows = s.execute(
            text("SELECT pricing_version, source FROM usage_records WHERE agent_id = :a"),
            {"a": AGENT},
        ).all()
        assert all(r[0] == "pricing-v1" for r in rows) and len(rows) == 4
        # aggregation per scope and day
        assert usage_for(s, "agent_daily", AGENT, DAY) == 850 + 13752 + 777
        assert usage_for(s, "agent_task", f"{AGENT}|task-usage-1", DAY) == 850 + 13752 + 777
        assert usage_for(s, "channel_daily", str(CHANNEL), DAY) == 850 + 13752 + 777
        assert usage_for(s, "agent_daily", AGENT, DAY + dt.timedelta(days=1)) == 0
        assert usage_for(s, "schedule_run", "run-none") == 0
        assert estimate_for(s, AGENT, "task_assignment", default=5) == -(-(850 + 13752 + 777) // 3)
        assert estimate_for(s, "agent-nobody", "task_assignment", default=5) == 5
        assert usage_summary(s, "agent_daily", AGENT, DAY).cost_units == 850 + 13752 + 777


def test_bus_report_usage_command(engine: Engine) -> None:
    with Session(engine) as s, s.begin():
        principal = Principal("acct-usage", str(ACTOR), "agent", "fp-usage", agent_id=AGENT)
        ctx = CommandContext(s, _store(s), None, CLOCK, principal, str(WS), "corr-bus", "idem-1")
        res = execute(
            ReportUsage(
                agent_id=AGENT,
                work_item_id="wi-usage-1",
                usage=_usage("generic-small"),
                task_id="task-usage-1",
            ),
            ctx,
        )
        assert res.data["cost_units"] == 850 and res.aggregate_type == "usage_record"
        with pytest.raises(CommandError) as exc:
            execute(ReportUsage(agent_id=AGENT, work_item_id="wi-usage-1", usage=None), ctx)
        assert exc.value.code == "USAGE_REQUIRED" and exc.value.status == 400
        # another agent's usage needs a permission; without an authorizer that is denied
        with pytest.raises(CommandError) as exc2:
            execute(
                ReportUsage(
                    agent_id="agent-other", work_item_id=None, usage=_usage("generic-small")
                ),
                ctx,
            )
        assert exc2.value.code == "POLICY_DENIED"


def _reserve(
    s: Session, st: PostgresEventStore, scope: BudgetScope, estimate: int, wi: str
) -> Reservation:
    return reserve(
        s,
        st,
        workspace_id=str(WS),
        actor_account_id=str(ACTOR),
        scope=scope,
        limit_cost_units=100,
        estimate=estimate,
        work_item_id=wi,
        correlation_id="corr-b",
        clock=CLOCK,
    )


def test_reservation_allow_allow_reject_and_events(engine: Engine) -> None:
    scope = BudgetScope("agent_task", "agent-budget-1|task-b")
    with Session(engine) as s, s.begin():
        st = _store(s)
        r1 = _reserve(s, st, scope, 99, "wi-b1")
        assert r1.estimated_cost_units == 99
        release(s, r1.reservation_id, CLOCK)
        r2 = _reserve(s, st, scope, 100, "wi-b2")
        release(s, r2.reservation_id, CLOCK)
        with pytest.raises(BudgetExceededError) as exc:
            _reserve(s, st, scope, 101, "wi-b3")
        assert exc.value.code == "BUDGET_EXCEEDED" and exc.value.status == 409
        assert (
            exc.value.extra["limit_cost_units"] == 100
            and exc.value.extra["requested_cost_units"] == 101
        )
        events = st.stream(str(WS), "budget", scope.aggregate_id())
        assert [e["type"] for e in events] == [
            "BUDGET_RESERVED",
            "BUDGET_RESERVED",
            "BUDGET_EXCEEDED",
        ]
        assert (
            events[-1]["payload"]["limit_cost_units"] == 100
            and events[-1]["payload"]["requested_cost_units"] == 101
        )
        # an identical retry of the rejected reservation replays the same Event (exactly once)
        outcome = try_reserve(
            s,
            st,
            workspace_id=str(WS),
            actor_account_id=str(ACTOR),
            scope=scope,
            limit_cost_units=100,
            estimate=101,
            work_item_id="wi-b3",
            correlation_id="corr-b",
            clock=CLOCK,
        )
        assert not outcome.reserved and outcome.event_id == events[-1]["event_id"]
        assert len(st.stream(str(WS), "budget", scope.aggregate_id())) == 3


def test_settlement_overrun_blocks_next_side_effect(engine: Engine) -> None:
    scope = BudgetScope("agent_daily", "agent-budget-2")
    with Session(engine) as s, s.begin():
        st = _store(s)
        r = reserve(
            s,
            st,
            workspace_id=str(WS),
            actor_account_id=str(ACTOR),
            scope=scope,
            limit_cost_units=1000,
            estimate=300,
            work_item_id="wi-s1",
            correlation_id="corr-s",
            clock=CLOCK,
        )
        assert settle(s, r.reservation_id, 250, 1000, CLOCK) == "settled"
        assert_not_overrun(s, scope, CLOCK)
        r2 = reserve(
            s,
            st,
            workspace_id=str(WS),
            actor_account_id=str(ACTOR),
            scope=scope,
            limit_cost_units=1000,
            estimate=300,
            work_item_id="wi-s2",
            correlation_id="corr-s",
            clock=CLOCK,
        )
        # actual usage recorded for the scope counts as used; overrun when actual > available
        record_usage(
            s,
            workspace_id=str(WS),
            account_id=str(ACTOR),
            agent_id="agent-budget-2",
            work_item_id=None,
            usage=_usage("generic-small", cost_units=900),
            clock=CLOCK,
        )
        assert (
            settle(s, r2.reservation_id, 200, 1000, CLOCK) == "exceeded"
        )  # available = 1000 - 900 = 100 < 200
        with pytest.raises(CommandError) as exc:
            assert_not_overrun(s, scope, CLOCK)
        assert exc.value.code == "BUDGET_EXCEEDED"
        with pytest.raises(CommandError) as exc2:
            settle(s, r2.reservation_id, 1, 1000, CLOCK)
        assert exc2.value.code == "BUDGET_RESERVATION_NOT_OPEN"


def test_concurrent_reservations_never_exceed_the_limit(engine: Engine) -> None:
    fresh_channel = str(uuid.uuid4())  # no usage recorded for it -> used_today = 0
    scope = BudgetScope("channel_daily", fresh_channel)
    n, limit, estimate = 10, 1000, 300  # at most 3 reservations can fit
    barrier = threading.Barrier(n)
    outcomes: list[bool] = []
    errors: list[Exception] = []

    def worker(i: int) -> None:
        with Session(engine) as s, s.begin():
            barrier.wait()
            try:
                out = try_reserve(
                    s,
                    _store(s),
                    workspace_id=str(WS),
                    actor_account_id=str(ACTOR),
                    scope=scope,
                    limit_cost_units=limit,
                    estimate=estimate,
                    work_item_id=f"wi-c{i}",
                    correlation_id="corr-c",
                    clock=CLOCK,
                )
                outcomes.append(out.reserved)
            except Exception as exc:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert outcomes.count(True) == 3 and outcomes.count(False) == 7
    with Session(engine) as s:
        reserved = s.execute(
            text(
                "SELECT COALESCE(SUM(estimated_cost_units),0) FROM budget_reservations WHERE "
                "scope_type='channel_daily' AND scope_id=:c AND status='reserved'"
            ),
            {"c": str(CHANNEL)},
        ).scalar_one()
        assert int(reserved) <= limit
        events = _store(s).stream(str(WS), "budget", scope.aggregate_id())
        assert sum(e["type"] == "BUDGET_EXCEEDED" for e in events) == 7
        assert sum(e["type"] == "BUDGET_RESERVED" for e in events) == 3
