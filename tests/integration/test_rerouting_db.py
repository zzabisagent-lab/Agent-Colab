"""V-P3-04 (unsupported capability → CAPABILITY_UNSUPPORTED with zero side effects, one
reselection or WAITING), V-P3-20 (assignee offline/revoked before and during execution →
reassigned with history, resume_context, zero duplicate side effects; WAITING without candidate)
and V-P3-25 (120-second accept timeout → one reassignment with a history revision; WAITING when
none; rejection codes)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

import server.application.documents  # noqa: F401
from server.agents import rerouting
from server.application import tasks as tk
from server.application import work as wk
from server.application.authz import AllowAllAuthorizer
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore
from server.work import timeouts
from tests.integration.phase3_seed import CRITERIA, Seed, event_types, status_of

pytestmark = pytest.mark.db
SEED = Seed("rr")
T0 = dt.datetime(2026, 5, 3, 9, 0, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    SEED.create(eng)
    with eng.begin() as c:
        SEED.add_agent(c, "acct-rr-a", capacity=50)
        SEED.add_agent(c, "acct-rr-b", capacity=50)
        SEED.add_agent(c, "acct-rr-c", capacity=3, online=False)  # comes online in one test
    yield eng
    eng.dispose()


def _clock() -> FixedClock:
    return FixedClock(T0)


def _run(engine: Engine, cmd: Any, who: str, key: str, clock: FixedClock) -> Any:
    return SEED.run(engine, cmd, SEED.principal(who), key, clock)


def _task(engine: Engine, key: str, clock: FixedClock, assignee: str = "acct-rr-a") -> str:
    tid = str(
        _run(
            engine,
            tk.CreateTask("reroute me", str(SEED.channel), "research", criteria=CRITERIA),
            "acct-rr-human",
            f"{key}-create",
            clock,
        ).resource_id
    )
    _run(engine, tk.DelegateTask(tid, assignee), "acct-rr-human", f"{key}-delegate", clock)
    return tid


def _items(engine: Engine, task_id: str) -> list[tuple[str, str, dict[str, Any]]]:
    with Session(engine) as s:
        return [
            (str(r[0]), str(r[1]), dict(r[2]))
            for r in s.execute(
                text(
                    "SELECT agent_id, status, payload FROM work_items WHERE task_id = :t "
                    "ORDER BY created_at, agent_id"
                ),
                {"t": task_id},
            ).all()
        ]


def _assignments(engine: Engine, task_id: str) -> list[tuple[int, str]]:
    with Session(engine) as s:
        return [
            (int(r[0]), str(r[1]))
            for r in s.execute(
                text(
                    "SELECT revision, reason_code FROM task_assignments WHERE task_id = :t "
                    "ORDER BY revision"
                ),
                {"t": task_id},
            ).all()
        ]


def _set_agent(engine: Engine, name: str, **cols: Any) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in cols)
    with Session(engine) as s, s.begin():
        s.execute(
            text(f"UPDATE agents SET {sets} WHERE agent_id = :g"),  # noqa: S608 - test-only column list
            {**cols, "g": SEED.agents[name]},
        )


def _poll_ack(
    engine: Engine, who: str, key: str, clock: FixedClock, task_id: str | None = None
) -> list[dict[str, Any]]:
    """Poll the Agent's inbox and ack the items of ``task_id`` (all when None)."""
    res = _run(engine, wk.WorkPoll(SEED.agents[who], max_items=50), who, f"{key}-poll", clock)
    items = [it for it in res.data["items"] if task_id is None or it["task_id"] == task_id]
    for it in items:
        _run(engine, wk.WorkAck(it["work_item_id"]), who, f"{key}-ack-{it['work_item_id']}", clock)
    return items


@pytest.mark.parametrize("code", ["CAPABILITY_UNSUPPORTED", "CAPACITY", "POLICY", "OTHER"])
def test_rejection_codes_reroute_once_to_the_next_candidate(engine: Engine, code: str) -> None:
    clock = _clock()
    tid = _task(engine, f"rej-{code}", clock)
    items = _poll_ack(engine, "acct-rr-a", f"rej-{code}", clock, tid)
    assert len(items) == 1 and items[0]["task_id"] == tid
    before = event_types(engine, tid)
    _run(
        engine,
        wk.WorkReject(items[0]["work_item_id"], code),
        "acct-rr-a",
        f"rej-{code}-reject",
        clock,
    )
    # exactly one reassignment to b with a history revision; a's item is REJECTED, b's is QUEUED
    assert status_of(engine, tid) == "DELEGATED"
    assert _assignments(engine, tid) == [(1, "DELEGATED"), (2, f"REROUTE_{code}")]
    assert event_types(engine, tid) == [*before, "TASK_REASSIGNED"]
    assert [(a, s) for a, s, _ in _items(engine, tid)] == [
        ("agent-rr-a", "REJECTED"),
        ("agent-rr-b", "QUEUED"),
    ]
    resume = _items(engine, tid)[1][2]["resume_context"]
    assert resume["previous_assignee_account_id"] == str(SEED.account("acct-rr-a"))
    assert resume["reroute_reason"] == code and resume["completed_steps"] == []
    with Session(engine) as s:
        decision = s.execute(
            text("SELECT purpose, selected_agent_id FROM routing_decisions WHERE task_id = :t"),
            {"t": tid},
        ).all()
    assert decision == [("reroute", "agent-rr-b")]


def test_unsupported_without_fallback_goes_waiting(engine: Engine) -> None:
    """V-P3-04: no eligible fallback → WAITING, zero duplicate side effects."""
    clock = _clock()
    tid = _task(engine, "nofb", clock)
    items = _poll_ack(engine, "acct-rr-a", "nofb", clock, tid)
    _set_agent(engine, "acct-rr-b", online=False)
    try:
        _run(
            engine,
            wk.WorkReject(items[0]["work_item_id"], "CAPABILITY_UNSUPPORTED"),
            "acct-rr-a",
            "nofb-reject",
            clock,
        )
    finally:
        _set_agent(engine, "acct-rr-b", online=True)
    assert status_of(engine, tid) == "WAITING"
    assert event_types(engine, tid)[-1] == "TASK_WAITING"
    assert _assignments(engine, tid) == [(1, "DELEGATED")]
    assert [(a, s) for a, s, _ in _items(engine, tid)] == [("agent-rr-a", "REJECTED")]
    with Session(engine) as s:
        reason = s.execute(
            text("SELECT reason_code FROM routing_decisions WHERE task_id = :t"), {"t": tid}
        ).scalar_one()
    assert reason == "NO_CANDIDATE"


def test_accept_timeout_reroutes_once_then_waiting(engine: Engine) -> None:
    """V-P3-25: 120 s without acceptance → one reassignment; a second timeout → WAITING."""
    clock = _clock()
    tid = _task(engine, "to", clock)
    _poll_ack(engine, "acct-rr-a", "to", clock, tid)  # acked, never accepted
    clock.advance(dt.timedelta(seconds=119))
    with Session(engine) as s, s.begin():
        store = PostgresEventStore(s, clock=clock)
        report = timeouts.sweep(
            s,
            store,
            clock=clock,
            actor_account_id=str(SEED.account("acct-rr-system")),
            agent_id=SEED.agents["acct-rr-a"],
        )
        assert report.reroute_required == []
    clock.advance(dt.timedelta(seconds=2))
    with Session(engine) as s, s.begin():
        store = PostgresEventStore(s, clock=clock)
        report = timeouts.sweep(
            s,
            store,
            clock=clock,
            actor_account_id=str(SEED.account("acct-rr-system")),
            agent_id=SEED.agents["acct-rr-a"],
        )
        assert len(report.reroute_required) == 1
        outcomes = rerouting.process_sweep(
            s,
            store,
            report,
            clock=clock,
            workspace_id=str(SEED.ws),
            actor=SEED.principal("acct-rr-system"),
            authorizer=AllowAllAuthorizer(),
        )
    assert [o.code for o in outcomes] == ["REASSIGNED"]
    assert _assignments(engine, tid) == [(1, "DELEGATED"), (2, "REROUTE_ACCEPT_TIMEOUT")]
    assert [(a, s) for a, s, _ in _items(engine, tid)] == [
        ("agent-rr-a", "CANCELLED"),
        ("agent-rr-b", "QUEUED"),
    ]
    # b acks but never accepts either: the single re-route is used up → WAITING
    b_items = _poll_ack(engine, "acct-rr-b", "to-b", clock, tid)
    clock.advance(dt.timedelta(seconds=121))
    with Session(engine) as s, s.begin():
        store = PostgresEventStore(s, clock=clock)
        report = timeouts.sweep(
            s,
            store,
            clock=clock,
            actor_account_id=str(SEED.account("acct-rr-system")),
            agent_id=SEED.agents["acct-rr-b"],
            reroute_counts={b_items[0]["work_item_id"]: 1},
        )
        assert [o.work_item_id for o in report.waiting_required] == [b_items[0]["work_item_id"]]
        outcomes = rerouting.process_sweep(
            s,
            store,
            report,
            clock=clock,
            workspace_id=str(SEED.ws),
            actor=SEED.principal("acct-rr-system"),
            authorizer=AllowAllAuthorizer(),
        )
    assert [o.code for o in outcomes] == ["WAITING"]
    assert status_of(engine, tid) == "WAITING"
    assert _assignments(engine, tid) == [(1, "DELEGATED"), (2, "REROUTE_ACCEPT_TIMEOUT")]
    assert all(s == "CANCELLED" for _, s, _ in _items(engine, tid))


def test_offline_before_and_during_execution(engine: Engine) -> None:
    """V-P3-20: offline/revoke before execution and during execution → reassigned with history and
    resume_context; already-started side effects are handed over, never repeated."""
    clock = _clock()
    # before execution (DELEGATED): a goes offline
    tid1 = _task(engine, "off1", clock)
    with Session(engine) as s, s.begin():
        out = rerouting.on_agent_unavailable(
            s,
            PostgresEventStore(s, clock=clock),
            "agent-rr-a",
            "AGENT_OFFLINE",
            clock=clock,
            actor=SEED.principal("acct-rr-system"),
            authorizer=AllowAllAuthorizer(),
        )
    assert {o.task_id: o.code for o in out}[tid1] == "REASSIGNED"
    assert _assignments(engine, tid1) == [(1, "DELEGATED"), (2, "REROUTE_AGENT_OFFLINE")]
    # during execution (RUNNING with progress): a is revoked
    tid2 = _task(engine, "off2", clock)
    _poll_ack(engine, "acct-rr-a", "off2", clock, tid2)
    _run(engine, tk.AcceptTask(tid2), "acct-rr-a", "off2-accept", clock)
    _run(engine, tk.StartTask(tid2), "acct-rr-a", "off2-start", clock)
    _run(
        engine,
        tk.ReportProgress(tid2, "step 1 done: fetched sources"),
        "acct-rr-a",
        "off2-p1",
        clock,
    )
    _run(
        engine,
        tk.ReportProgress(tid2, "step 2 done: drafted outline"),
        "acct-rr-a",
        "off2-p2",
        clock,
    )
    with Session(engine) as s, s.begin():
        out = rerouting.on_agent_unavailable(
            s,
            PostgresEventStore(s, clock=clock),
            "agent-rr-a",
            "AGENT_REVOKED",
            clock=clock,
            actor=SEED.principal("acct-rr-system"),
            authorizer=AllowAllAuthorizer(),
        )
    assert {o.task_id: o.code for o in out}[tid2] == "REASSIGNED"
    assert status_of(engine, tid2) == "DELEGATED"  # the new assignee accepts again
    assert _assignments(engine, tid2) == [(1, "DELEGATED"), (2, "REROUTE_AGENT_REVOKED")]
    items = _items(engine, tid2)
    assert [(a, s) for a, s, _ in items] == [("agent-rr-a", "CANCELLED"), ("agent-rr-b", "QUEUED")]
    resume = items[1][2]["resume_context"]
    assert resume["started"] is True and resume["last_progress"] == "step 2 done: drafted outline"
    assert [st["summary"] for st in resume["completed_steps"]] == [
        "step 1 done: fetched sources",
        "step 2 done: drafted outline",
    ]
    # b continues from the handover; the history keeps both revisions and the progress Events
    _poll_ack(engine, "acct-rr-b", "off2-b", clock, tid2)
    _run(engine, tk.AcceptTask(tid2), "acct-rr-b", "off2-b-accept", clock)
    _run(engine, tk.StartTask(tid2), "acct-rr-b", "off2-b-start", clock)
    types = event_types(engine, tid2)
    assert types.count("TASK_PROGRESS_REPORTED") == 2 and types.count("TASK_REASSIGNED") == 1
    # no candidate at all: b and c unavailable → WAITING, delegator/channel notified by rule
    tid3 = _task(engine, "off3", clock, assignee="acct-rr-b")
    _set_agent(engine, "acct-rr-a", online=False)
    try:
        with Session(engine) as s, s.begin():
            out = rerouting.on_agent_unavailable(
                s,
                PostgresEventStore(s, clock=clock),
                "agent-rr-b",
                "AGENT_OFFLINE",
                clock=clock,
                actor=SEED.principal("acct-rr-system"),
                authorizer=AllowAllAuthorizer(),
            )
    finally:
        _set_agent(engine, "acct-rr-a", online=True)
    codes = {o.task_id: o.code for o in out}
    assert codes[tid3] == "WAITING" and status_of(engine, tid3) == "WAITING"
