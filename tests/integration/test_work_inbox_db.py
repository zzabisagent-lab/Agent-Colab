"""V-P1-29: durable inbox — 3 redeliveries after 60 s ack timeouts then EXPIRED, exactly-once
results with audited duplicates, deadline expiry, reconnect redelivery, concurrent polls."""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application import work as wk
from server.application.authz import AllowAllAuthorizer
from server.application.bus import CommandContext, CommandError, Principal, execute
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore
from server.work import inbox, receipts, timeouts
from server.work.state import WorkItemError, WorkItemState

pytestmark = pytest.mark.db

WS = uuid.uuid4()
AGENT_ACCOUNT = uuid.uuid4()
OTHER_ACCOUNT = uuid.uuid4()
SERVICE = uuid.uuid4()
AGENT = "agent-inbox-1"
OTHER = "agent-inbox-2"
T0 = dt.datetime(2026, 4, 1, 12, 0, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-inbox', 'inbox')"
            ),
            {"i": WS},
        )
        for acc, name, typ in (
            (AGENT_ACCOUNT, "acct-inbox-agent", "agent"),
            (OTHER_ACCOUNT, "acct-inbox-other", "agent"),
            (SERVICE, "acct-inbox-service", "service"),
        ):
            c.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc, "a": name, "w": WS, "t": typ},
            )
        for agent, acc in ((AGENT, AGENT_ACCOUNT), (OTHER, OTHER_ACCOUNT)):
            c.execute(
                text(
                    "INSERT INTO agents (id, agent_id, workspace_id, account_id, adapter_type, "
                    "status, display_name) VALUES (:i, :g, :w, :a, 'mcp', 'active', :g)"
                ),
                {"i": uuid.uuid4(), "g": agent, "w": WS, "a": acc},
            )
    yield eng
    eng.dispose()


def _store(s: Session, clock: FixedClock) -> PostgresEventStore:
    return PostgresEventStore(s, clock=clock)


def _enqueue(
    s: Session,
    clock: FixedClock,
    key: str,
    *,
    kind: str = "invoke",
    agent: str = AGENT,
    deadline: dt.datetime | None = None,
) -> inbox.WorkItem:
    return inbox.enqueue(
        s,
        _store(s, clock),
        workspace_id=str(WS),
        kind=kind,
        agent_id=agent,
        payload={"tool": "echo", "input": {"n": 1}},
        deadline=deadline or clock.now() + dt.timedelta(hours=4),
        expected_result_schema="colab.work-result.v1",
        correlation_id=f"corr-{key}",
        idempotency_key=key,
        actor_account_id=str(SERVICE),
        clock=clock,
        task_id="task-inbox-1",
    )


def _result_doc(wid: str, *, usage: bool = True, n: int = 1) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_id": "colab.work-result.v1",
        "work_item_id": wid,
        "correlation_id": "corr",
        "status": "SUCCEEDED",
        "result": {"n": n},
        "events": [],
        "artifacts": [],
    }
    if usage:
        doc["usage"] = {
            "model": "m",
            "input_tokens": 1,
            "output_tokens": 1,
            "tool_calls": 0,
            "wall_time_ms": 5,
        }
    else:
        doc["usage_unavailable"] = {"reason": "ADAPTER_NO_METERING"}
    return doc


def _events(s: Session, wid: str) -> list[str]:
    return [e["type"] for e in PostgresEventStore(s).stream(str(WS), "work_item", wid)]


def test_three_redeliveries_after_ack_timeouts_then_expired(engine: Engine) -> None:
    clock = FixedClock(T0)
    with Session(engine) as s, s.begin():
        item = _enqueue(s, clock, "k-redeliver")
        assert item.status is WorkItemState.QUEUED and item.delivery_count == 0
        assert _enqueue(s, clock, "k-redeliver").work_item_id == item.work_item_id  # idempotent
        wid = item.work_item_id
    deliveries = 0
    for expected_count in (1, 2, 3, 4):
        with Session(engine) as s, s.begin():
            res = inbox.poll(
                s, _store(s, clock), AGENT, actor_account_id=str(AGENT_ACCOUNT), clock=clock
            )
            assert [i.work_item_id for i in res.items] == [wid]
            assert res.items[0].status is WorkItemState.DELIVERED
            assert res.items[0].delivery_count == expected_count
            deliveries += len(res.delivered_event_ids)
        # polling again before the timeout redelivers the same delivery (reconnect), no new count
        with Session(engine) as s, s.begin():
            again = inbox.poll(
                s, _store(s, clock), AGENT, actor_account_id=str(AGENT_ACCOUNT), clock=clock
            )
            assert (
                again.items[0].delivery_count == expected_count and again.delivered_event_ids == []
            )
        clock.advance(dt.timedelta(seconds=59))
        with Session(engine) as s, s.begin():
            report = timeouts.sweep(s, _store(s, clock), clock=clock, actor_account_id=str(SERVICE))
            assert report.outcomes == []  # 59 s: still awaiting ack
        clock.advance(dt.timedelta(seconds=1))
        with Session(engine) as s, s.begin():
            report = timeouts.sweep(s, _store(s, clock), clock=clock, actor_account_id=str(SERVICE))
            if expected_count < 4:
                assert [o.action for o in report.outcomes] == ["REDELIVER"]
                assert inbox.load(s, wid).status is WorkItemState.QUEUED
            else:
                assert [(o.action, o.reason) for o in report.outcomes] == [
                    ("EXPIRE", "ACK_TIMEOUT")
                ]
    with Session(engine) as s:
        final = inbox.load(s, wid)
        assert final.status is WorkItemState.EXPIRED and final.delivery_count == 4
        assert deliveries == 4  # 1 delivery + exactly 3 redeliveries
        types = _events(s, wid)
        assert types.count("WORK_ITEM_DELIVERED") == 4 and types.count("WORK_ITEM_EXPIRED") == 1
        assert [r.receipt_kind for r in receipts.receipts_of(s, wid)] == ["delivery"] * 4
    with Session(engine) as s, s.begin():  # no further deliveries or sweeps touch it
        assert (
            inbox.poll(
                s, _store(s, clock), AGENT, actor_account_id=str(AGENT_ACCOUNT), clock=clock
            ).items
            == []
        )
        clock.advance(dt.timedelta(hours=1))
        assert (
            timeouts.sweep(s, _store(s, clock), clock=clock, actor_account_id=str(SERVICE)).outcomes
            == []
        )
        with pytest.raises(WorkItemError) as exc:
            inbox.ack(
                s, _store(s, clock), wid, AGENT, actor_account_id=str(AGENT_ACCOUNT), clock=clock
            )
        assert exc.value.code == "WORK_ITEM_TRANSITION_INVALID"


def test_results_exactly_once_and_duplicates_audited(engine: Engine) -> None:
    clock = FixedClock(T0)
    with Session(engine) as s, s.begin():
        wid = _enqueue(s, clock, "k-result").work_item_id
        inbox.poll(s, _store(s, clock), AGENT, actor_account_id=str(AGENT_ACCOUNT), clock=clock)
        inbox.ack(s, _store(s, clock), wid, AGENT, actor_account_id=str(AGENT_ACCOUNT), clock=clock)
        inbox.ack(
            s, _store(s, clock), wid, AGENT, actor_account_id=str(AGENT_ACCOUNT), clock=clock
        )  # idempotent
        inbox.start(
            s, _store(s, clock), wid, AGENT, actor_account_id=str(AGENT_ACCOUNT), clock=clock
        )
        first = inbox.result(
            s,
            _store(s, clock),
            wid,
            AGENT,
            _result_doc(wid),
            actor_account_id=str(AGENT_ACCOUNT),
            clock=clock,
        )
        assert (
            first.code == "RESULT_ACCEPTED" and first.item.status is WorkItemState.RESULT_RECEIVED
        )
        same = inbox.result(
            s,
            _store(s, clock),
            wid,
            AGENT,
            _result_doc(wid),
            actor_account_id=str(AGENT_ACCOUNT),
            clock=clock,
        )
        different = inbox.result(
            s,
            _store(s, clock),
            wid,
            AGENT,
            _result_doc(wid, n=2),
            actor_account_id=str(AGENT_ACCOUNT),
            clock=clock,
        )
        for dup in (same, different):
            assert dup.code == "DUPLICATE_RESULT_IGNORED" and dup.result_ref == first.result_ref
            assert dup.receipt_id == first.receipt_id and dup.event_id is None
        assert inbox.load(s, wid).status is WorkItemState.RESULT_RECEIVED
        kinds = [r.receipt_kind for r in receipts.receipts_of(s, wid)]
        assert kinds.count("result") == 1 and kinds.count("duplicate_result") == 2
        audits = s.execute(
            text(
                "SELECT count(*) FROM audit_events "
                "WHERE action = 'work.duplicate_result_ignored' AND target_id = :w"
            ),
            {"w": wid},
        ).scalar_one()
        assert audits == 2
        assert _events(s, wid).count("WORK_ITEM_RESULT_RECEIVED") == 1
        assert _events(s, wid).count("WORK_ITEM_ACKED") == 1


def test_result_without_usage_or_reason_and_wrong_owner_are_rejected(engine: Engine) -> None:
    clock = FixedClock(T0)
    with Session(engine) as s, s.begin():
        wid = _enqueue(s, clock, "k-invalid").work_item_id
        inbox.poll(s, _store(s, clock), AGENT, actor_account_id=str(AGENT_ACCOUNT), clock=clock)
        bad = _result_doc(wid)
        del bad["usage"]
        with pytest.raises(WorkItemError) as exc:
            inbox.result(
                s,
                _store(s, clock),
                wid,
                AGENT,
                bad,
                actor_account_id=str(AGENT_ACCOUNT),
                clock=clock,
            )
        assert exc.value.code == "WORK_RESULT_SCHEMA_INVALID"
        with pytest.raises(WorkItemError) as exc2:
            inbox.result(
                s,
                _store(s, clock),
                wid,
                OTHER,
                _result_doc(wid),
                actor_account_id=str(OTHER_ACCOUNT),
                clock=clock,
            )
        assert exc2.value.code == "WORK_ITEM_NOT_OWNER"
        assert inbox.load(s, wid).status is WorkItemState.DELIVERED
        # a result on a DELIVERED item implies the ack and is accepted once
        ok = inbox.result(
            s,
            _store(s, clock),
            wid,
            AGENT,
            _result_doc(wid, usage=False),
            actor_account_id=str(AGENT_ACCOUNT),
            clock=clock,
        )
        assert ok.code == "RESULT_ACCEPTED" and _events(s, wid)[-2:] == [
            "WORK_ITEM_ACKED",
            "WORK_ITEM_RESULT_RECEIVED",
        ]


def test_deadline_expiry_and_reject_and_cancel(engine: Engine) -> None:
    clock = FixedClock(T0)
    with Session(engine) as s, s.begin():
        short = _enqueue(s, clock, "k-deadline", deadline=T0 + dt.timedelta(minutes=5)).work_item_id
        rejected = _enqueue(s, clock, "k-reject").work_item_id
        cancelled = _enqueue(s, clock, "k-cancel").work_item_id
        inbox.poll(
            s,
            _store(s, clock),
            AGENT,
            actor_account_id=str(AGENT_ACCOUNT),
            clock=clock,
            max_items=100,
        )
        with pytest.raises(WorkItemError) as exc:
            inbox.reject(
                s,
                _store(s, clock),
                rejected,
                AGENT,
                "NOPE",
                actor_account_id=str(AGENT_ACCOUNT),
                clock=clock,
            )
        assert exc.value.code == "WORK_ITEM_REJECTION_CODE_INVALID"
        r = inbox.reject(
            s,
            _store(s, clock),
            rejected,
            AGENT,
            "CAPACITY",
            actor_account_id=str(AGENT_ACCOUNT),
            clock=clock,
        )
        c = inbox.cancel(
            s,
            _store(s, clock),
            cancelled,
            "TASK_CANCELLED",
            actor_account_id=str(SERVICE),
            clock=clock,
        )
        assert r.status is WorkItemState.REJECTED and c.status is WorkItemState.CANCELLED
    clock.advance(dt.timedelta(minutes=5))
    with Session(engine) as s, s.begin():
        report = timeouts.sweep(
            s, _store(s, clock), clock=clock, actor_account_id=str(SERVICE), agent_id=AGENT
        )
        assert [(o.work_item_id, o.action, o.reason) for o in report.expired] == [
            (short, "EXPIRE", "DEADLINE")
        ]
        assert inbox.load(s, short).status is WorkItemState.EXPIRED
        assert "WORK_ITEM_EXPIRED" in _events(s, short)


def test_assignment_accept_timeout_yields_reroute_outcome(engine: Engine) -> None:
    clock = FixedClock(T0)
    with Session(engine) as s, s.begin():
        wid = _enqueue(s, clock, "k-assign", kind="task_assignment", agent=OTHER).work_item_id
        inbox.poll(s, _store(s, clock), OTHER, actor_account_id=str(OTHER_ACCOUNT), clock=clock)
        inbox.ack(s, _store(s, clock), wid, OTHER, actor_account_id=str(OTHER_ACCOUNT), clock=clock)
    clock.advance(dt.timedelta(seconds=120))
    with Session(engine) as s, s.begin():
        report = timeouts.sweep(
            s, _store(s, clock), clock=clock, actor_account_id=str(SERVICE), agent_id=OTHER
        )
        assert [o.action for o in report.reroute_required] == ["REROUTE_REQUIRED"]
        waiting = timeouts.sweep(
            s,
            _store(s, clock),
            clock=clock,
            actor_account_id=str(SERVICE),
            agent_id=OTHER,
            reroute_counts={wid: 1},
        )
        assert [o.action for o in waiting.waiting_required] == ["WAITING_REQUIRED"]
        assert inbox.load(s, wid).status is WorkItemState.ACKED  # untouched: P3-14 re-routes


def test_concurrent_polls_never_deliver_the_same_item_twice(engine: Engine) -> None:
    clock = FixedClock(T0)
    with Session(engine) as s, s.begin():
        wids = [_enqueue(s, clock, f"k-conc-{i}", agent=OTHER).work_item_id for i in range(6)]
    barrier = threading.Barrier(6)
    seen: list[tuple[str, int]] = []
    lock = threading.Lock()

    def worker() -> None:
        with Session(engine) as s, s.begin():
            barrier.wait()
            res = inbox.poll(
                s,
                _store(s, clock),
                OTHER,
                actor_account_id=str(OTHER_ACCOUNT),
                clock=clock,
                max_items=2,
            )
            with lock:
                seen.extend((i.work_item_id, i.delivery_count) for i in res.items)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = [w for w, _ in seen]
    assert sorted(ids) == sorted(wids) and len(set(ids)) == 6
    assert all(count == 1 for _, count in seen)
    with Session(engine) as s:
        assert all(_events(s, w).count("WORK_ITEM_DELIVERED") == 1 for w in wids)


def test_bus_commands_bind_the_inbox_to_the_credential(engine: Engine) -> None:
    clock = FixedClock(T0)

    def ctx(s: Session, account: uuid.UUID, public: str, key: str) -> CommandContext:
        return CommandContext(
            session=s,
            store=_store(s, clock),
            authorizer=AllowAllAuthorizer(),
            clock=clock,
            principal=Principal(public, str(account), "agent", "fp"),
            workspace_id=str(WS),
            correlation_id="corr-bus",
            idempotency_key=key,
        )

    with Session(engine) as s, s.begin():
        queued = execute(
            wk.QueueWorkItem(
                kind="invoke",
                agent_id=AGENT,
                payload={"tool": "x"},
                deadline="2026-04-01T20:00:00Z",
            ),
            ctx(s, SERVICE, "acct-inbox-service", "bus-queue-1"),
        )
        wid = queued.resource_id
        polled = execute(
            wk.WorkPoll(agent_id=AGENT), ctx(s, AGENT_ACCOUNT, "acct-inbox-agent", "bus-poll-1")
        )
        assert [i["work_item_id"] for i in polled.data["items"]] == [wid] and polled.data[
            "delivered"
        ] == 1
        with pytest.raises(CommandError) as exc:  # other agent's inbox is not visible
            execute(
                wk.WorkPoll(agent_id=AGENT), ctx(s, OTHER_ACCOUNT, "acct-inbox-other", "bus-poll-2")
            )
        assert exc.value.code == "WORK_ITEM_NOT_OWNER" and exc.value.status == 404
        with pytest.raises(CommandError) as exc2:
            execute(
                wk.WorkAck(work_item_id=wid), ctx(s, OTHER_ACCOUNT, "acct-inbox-other", "bus-ack-x")
            )
        assert exc2.value.code == "WORK_ITEM_NOT_OWNER"
        execute(
            wk.WorkAck(work_item_id=wid), ctx(s, AGENT_ACCOUNT, "acct-inbox-agent", "bus-ack-1")
        )
        doc = _result_doc(wid)
        first = execute(
            wk.WorkResult(work_item_id=wid, result=doc),
            ctx(s, AGENT_ACCOUNT, "acct-inbox-agent", "bus-res-1"),
        )
        dup = execute(
            wk.WorkResult(work_item_id=wid, result=doc),
            ctx(s, AGENT_ACCOUNT, "acct-inbox-agent", "bus-res-2"),
        )
        assert (
            first.data["code"] == "RESULT_ACCEPTED"
            and dup.data["code"] == "DUPLICATE_RESULT_IGNORED"
        )
        assert dup.replayed and dup.data["result_ref"] == first.data["result_ref"]
        with pytest.raises(CommandError) as exc3:
            execute(
                wk.WorkPoll(agent_id=AGENT), ctx(s, SERVICE, "acct-inbox-service", "bus-poll-3")
            )
        assert exc3.value.code == "AGENT_NOT_FOUND"
