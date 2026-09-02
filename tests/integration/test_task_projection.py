"""P1-04 integration: Task commands through the bus (V-P1-27 flows, V-P1-09 terminal, V-P1-14
completion gate), sub-task edges/cycles, and projection rebuild equivalence (V-P1-10)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application import tasks as tk
from server.application.bus import (
    CommandContext,
    CommandError,
    CommandResult,
    Principal,
    execute,
)
from server.db.engine import make_engine
from server.domain.clock import SteppingClock
from server.domain.task import TaskStatus
from server.events.postgres_store import PostgresEventStore
from server.projections.runner import rebuild, snapshot_hash
from server.projections.tasks import load_state

pytestmark = pytest.mark.db

WS = uuid.uuid4()
CHANNEL = uuid.uuid4()
HUMAN = uuid.uuid4()
AGENT = uuid.uuid4()
VERIFIER = uuid.uuid4()
CLOCK = SteppingClock(dt.datetime(2026, 3, 1, tzinfo=dt.UTC))


class AllowAllAuthorizer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def require(
        self, session: Session, principal_account_uuid: str, permission: str, **scope: Any
    ) -> None:
        self.calls.append(permission)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-tasks', 'tasks')"
            ),
            {"i": WS},
        )
        for acc, name, typ in (
            (HUMAN, "acct-t-human", "human"),
            (AGENT, "acct-t-agent", "agent"),
            (VERIFIER, "acct-t-verifier", "agent"),
        ):
            c.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc, "a": name, "w": WS, "t": typ},
            )
        c.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name) "
                "VALUES (:i, 'chan-tasks', :w, 'work', 'tasks')"
            ),
            {"i": CHANNEL, "w": WS},
        )
    yield eng
    eng.dispose()


def _principal(acc: uuid.UUID, public: str, typ: str = "human") -> Principal:
    return Principal(
        account_id=public,
        account_uuid=str(acc),
        account_type=typ,
        credential_fingerprint=f"fp-{public}",
    )


def run(
    engine: Engine, cmd: Any, who: Principal, key: str, extras: dict[str, Any] | None = None
) -> CommandResult:
    with Session(engine) as s, s.begin():
        ctx = CommandContext(
            session=s,
            store=PostgresEventStore(s, clock=CLOCK),
            authorizer=AllowAllAuthorizer(),
            clock=CLOCK,
            principal=who,
            workspace_id=str(WS),
            correlation_id="corr-tasks",
            idempotency_key=key,
            extras=extras or {},
        )
        return execute(cmd, ctx)


def _status(engine: Engine, task_id: str) -> str:
    with Session(engine) as s:
        return str(
            s.execute(
                text("SELECT status FROM tasks_projection WHERE task_id = :t"), {"t": task_id}
            ).scalar()
        )


def _event_count(engine: Engine, task_id: str) -> int:
    with Session(engine) as s:
        return int(
            s.execute(
                text("SELECT count(*) FROM events WHERE task_id = :t"), {"t": task_id}
            ).scalar_one()
        )


HUMAN_P = _principal(HUMAN, "acct-t-human")
AGENT_P = _principal(AGENT, "acct-t-agent", "agent")
VERIFIER_P = _principal(VERIFIER, "acct-t-verifier", "agent")


def _drive_to_verifying(engine: Engine, task_id: str, prefix: str, verification_id: str) -> None:
    run(engine, tk.DelegateTask(task_id, "acct-t-agent"), HUMAN_P, f"{prefix}-delegate")
    run(engine, tk.AcceptTask(task_id), AGENT_P, f"{prefix}-accept")
    run(engine, tk.StartTask(task_id), AGENT_P, f"{prefix}-start")
    run(engine, tk.ReportProgress(task_id, "half"), AGENT_P, f"{prefix}-progress")
    run(engine, tk.SubmitImplementation(task_id, ("art-1",), 1), AGENT_P, f"{prefix}-submit")
    run(engine, tk.StartVerification(task_id, verification_id), HUMAN_P, f"{prefix}-verify")


def test_normal_flow_read_after_write_and_events(
    engine: Engine,
) -> None:  # V-P1-27 normal, V-P1-01 projection
    created = run(
        engine,
        tk.CreateTask("Write report", str(CHANNEL), "research"),
        HUMAN_P,
        "n-create",
        extras={"policy_snapshot": {"roles": ["worker@1"]}},
    )
    tid = created.resource_id
    assert created.aggregate_seq == 1 and _status(engine, tid) == "OPEN"
    _drive_to_verifying(engine, tid, "n", "vr-n-1")
    assert _status(engine, tid) == "VERIFYING"
    res = run(
        engine,
        tk.RecordVerificationResult(tid, "vr-n-1", "PASSED", evidence_refs=("art-1",)),
        VERIFIER_P,
        "n-pass",
    )
    assert res.data["status"] == "VERIFIED" and _status(engine, tid) == "VERIFIED"
    done = run(engine, tk.CompleteTask(tid, "doc-n-1"), HUMAN_P, "n-complete")
    assert done.data["status"] == "COMPLETED" and _status(engine, tid) == "COMPLETED"
    with Session(engine) as s:
        types = [
            r[0]
            for r in s.execute(
                text("SELECT type FROM events WHERE task_id = :t ORDER BY recorded_seq"), {"t": tid}
            )
        ]
        assert types == [
            "TASK_CREATED",
            "TASK_DELEGATED",
            "TASK_ACCEPTED",
            "TASK_STARTED",
            "TASK_PROGRESS_REPORTED",
            "IMPLEMENTATION_SUBMITTED",
            "TASK_VERIFICATION_STARTED",
            "VERIFICATION_PASSED",
            "TASK_COMPLETED",
        ]
        seqs = [
            r[0]
            for r in s.execute(
                text(
                    "SELECT aggregate_seq FROM events WHERE aggregate_id = :t "
                    "ORDER BY aggregate_seq"
                ),
                {"t": tid},
            )
        ]
        assert seqs == list(range(1, 9))
        assignment = s.execute(
            text("SELECT revision, reason_code FROM task_assignments WHERE task_id = :t"),
            {"t": tid},
        ).all()
        assert assignment == [(1, "DELEGATED")]
        state = load_state(s, tid)
        assert (
            state.policy_snapshot_hash
            and state.verification_status == "PASSED"
            and state.latest_progress == "half"
        )


def test_failed_returns_to_running_and_blocked_to_waiting(engine: Engine) -> None:  # V-P1-27
    tid = run(
        engine, tk.CreateTask("recheck", str(CHANNEL), "research"), HUMAN_P, "f-create"
    ).resource_id
    _drive_to_verifying(engine, tid, "f", "vr-f-1")
    run(
        engine,
        tk.RecordVerificationResult(tid, "vr-f-1", "FAILED", finding_ids=("F-1",)),
        VERIFIER_P,
        "f-fail",
    )
    assert _status(engine, tid) == "RUNNING"
    run(engine, tk.SubmitImplementation(tid, ("art-2",), 2), AGENT_P, "f-submit2")
    run(engine, tk.StartVerification(tid, "vr-f-2"), HUMAN_P, "f-verify2")
    run(
        engine,
        tk.RecordVerificationResult(tid, "vr-f-2", "BLOCKED", reason_code="ENV"),
        VERIFIER_P,
        "f-block",
    )
    assert _status(engine, tid) == "WAITING"
    # a stale result for the old verification is rejected and changes nothing
    with pytest.raises(CommandError) as exc:
        run(engine, tk.RecordVerificationResult(tid, "vr-f-1", "PASSED"), VERIFIER_P, "f-stale")
    assert exc.value.code == "TASK_TRANSITION_INVALID" and _status(engine, tid) == "WAITING"
    run(engine, tk.StartTask(tid), AGENT_P, "f-resume")
    assert _status(engine, tid) == "RUNNING"
    run(engine, tk.SubmitImplementation(tid, ("art-3",), 3), AGENT_P, "f-submit3")
    run(engine, tk.StartVerification(tid, "vr-f-3"), HUMAN_P, "f-verify3")
    run(engine, tk.RecordVerificationResult(tid, "vr-f-3", "PASSED"), VERIFIER_P, "f-pass")
    assert _status(engine, tid) == "VERIFIED"


def test_complete_before_verification_is_rejected(engine: Engine) -> None:  # V-P1-14
    tid = run(
        engine, tk.CreateTask("early", str(CHANNEL), "research"), HUMAN_P, "e-create"
    ).resource_id
    run(engine, tk.DelegateTask(tid, "acct-t-agent"), HUMAN_P, "e-delegate")
    for key in ("e-complete-1", "e-complete-2"):
        with pytest.raises(CommandError) as exc:
            run(engine, tk.CompleteTask(tid, "doc-x"), HUMAN_P, key)
        assert exc.value.code == "VERIFICATION_REQUIRED"
    assert _status(engine, tid) == "DELEGATED" and _event_count(engine, tid) == 2


def test_invalid_and_terminal_writes_have_zero_side_effects(
    engine: Engine,
) -> None:  # V-P1-09, V-P1-27
    tid = run(
        engine, tk.CreateTask("terminal", str(CHANNEL), "research"), HUMAN_P, "t-create"
    ).resource_id
    for cmd, key, code in (
        (tk.AcceptTask(tid), "t-accept-open", "TASK_TRANSITION_INVALID"),
        (tk.StartTask(tid), "t-start-open", "TASK_TRANSITION_INVALID"),
        (tk.SubmitImplementation(tid, ("a",)), "t-submit-open", "TASK_TRANSITION_INVALID"),
        (tk.RequestCancel(tid), "t-cancelreq-open", "TASK_TRANSITION_INVALID"),
    ):
        with pytest.raises(CommandError) as exc:
            run(engine, cmd, AGENT_P, key)
        assert exc.value.code == code
    assert _status(engine, tid) == "OPEN" and _event_count(engine, tid) == 1
    run(engine, tk.CancelTask(tid, "NOT_NEEDED"), HUMAN_P, "t-cancel")
    assert _status(engine, tid) == "CANCELLED"
    count = _event_count(engine, tid)
    for cmd, key in (
        (tk.DelegateTask(tid, "acct-t-agent"), "t-delegate-after"),
        (tk.CancelTask(tid), "t-cancel-again"),
        (tk.CompleteTask(tid, "doc"), "t-complete-after"),
        (tk.ReportProgress(tid, "x"), "t-progress-after"),
    ):
        with pytest.raises(CommandError) as exc:
            run(engine, cmd, HUMAN_P, key)
        assert exc.value.code == "TASK_TERMINAL"
    assert _status(engine, tid) == "CANCELLED" and _event_count(engine, tid) == count
    # completed tasks are equally immutable
    tid2 = run(
        engine, tk.CreateTask("done", str(CHANNEL), "research"), HUMAN_P, "t2-create"
    ).resource_id
    _drive_to_verifying(engine, tid2, "t2", "vr-t2")
    run(engine, tk.RecordVerificationResult(tid2, "vr-t2", "PASSED"), VERIFIER_P, "t2-pass")
    run(engine, tk.CompleteTask(tid2, "doc-t2"), HUMAN_P, "t2-complete")
    count2 = _event_count(engine, tid2)
    with pytest.raises(CommandError) as exc:
        run(engine, tk.CompleteTask(tid2, "doc-t2"), HUMAN_P, "t2-complete-again")
    assert exc.value.code == "TASK_TERMINAL"
    with pytest.raises(CommandError) as exc:
        run(engine, tk.StartTask(tid2), AGENT_P, "t2-rerun")
    assert exc.value.code == "TASK_TERMINAL"
    assert _status(engine, tid2) == "COMPLETED" and _event_count(engine, tid2) == count2


def test_idempotent_replay_of_a_command_returns_the_same_event(engine: Engine) -> None:
    tid = run(
        engine, tk.CreateTask("idem", str(CHANNEL), "research"), HUMAN_P, "i-create"
    ).resource_id
    first = run(engine, tk.DelegateTask(tid, "acct-t-agent"), HUMAN_P, "i-delegate")
    again = run(engine, tk.DelegateTask(tid, "acct-t-agent"), HUMAN_P, "i-delegate")
    assert again.replayed and again.event_id == first.event_id
    assert _event_count(engine, tid) == 2 and _status(engine, tid) == "DELEGATED"


def test_cancel_during_execution_and_accept_by_non_assignee(engine: Engine) -> None:
    tid = run(
        engine, tk.CreateTask("cancel", str(CHANNEL), "research"), HUMAN_P, "c-create"
    ).resource_id
    run(engine, tk.DelegateTask(tid, "acct-t-agent"), HUMAN_P, "c-delegate")
    with pytest.raises(CommandError) as exc:
        run(engine, tk.AcceptTask(tid), VERIFIER_P, "c-accept-wrong")
    assert exc.value.code == "TASK_NOT_ASSIGNEE"
    run(engine, tk.AcceptTask(tid), AGENT_P, "c-accept")
    run(engine, tk.StartTask(tid), AGENT_P, "c-start")
    run(engine, tk.RequestCancel(tid, "USER"), HUMAN_P, "c-req")
    assert _status(engine, tid) == "CANCEL_REQUESTED"
    run(engine, tk.CancelTask(tid, "USER"), HUMAN_P, "c-cancel")
    assert _status(engine, tid) == "CANCELLED"


def test_subtasks_edges_and_cycle_rejection(engine: Engine) -> None:
    root = run(
        engine, tk.CreateTask("root", str(CHANNEL), "research"), HUMAN_P, "s-root"
    ).resource_id
    child = run(engine, tk.CreateSubtask(root, "child", "research"), HUMAN_P, "s-child").resource_id
    grand = run(
        engine, tk.CreateSubtask(child, "grandchild", "research"), HUMAN_P, "s-grand"
    ).resource_id
    with Session(engine) as s:
        rows = s.execute(
            text(
                "SELECT child_task_id, parent_task_id, root_task_id, depth FROM task_edges "
                "WHERE root_task_id = :r ORDER BY depth"
            ),
            {"r": root},
        ).all()
        assert [tuple(r) for r in rows] == [(child, root, root, 1), (grand, child, root, 2)]
        assert (
            load_state(s, grand).delegation_depth == 2 and load_state(s, grand).root_task_id == root
        )
    with pytest.raises(CommandError) as exc:
        run(engine, tk.CreateSubtask(root, "self", "research", task_id=root), HUMAN_P, "s-self")
    assert exc.value.code == "TASK_GRAPH_CYCLE"
    # an ancestor cannot become a child of its descendant (DB trigger)
    with Session(engine) as s, s.begin():
        ctx = CommandContext(
            session=s,
            store=PostgresEventStore(s, clock=CLOCK),
            authorizer=AllowAllAuthorizer(),
            clock=CLOCK,
            principal=HUMAN_P,
            workspace_id=str(WS),
            correlation_id="c",
            idempotency_key="s-cycle",
        )
        created = s.execute(
            text("SELECT created_event_id FROM task_edges WHERE child_task_id = :c"), {"c": child}
        ).scalar_one()
        with pytest.raises(CommandError) as exc2:
            tk.link_subtask_edge(ctx, root, grand, root, 3, str(created))
        assert exc2.value.code == "TASK_GRAPH_CYCLE"
    with pytest.raises(CommandError) as exc3:
        run(engine, tk.CreateSubtask("task-missing", "x", "research"), HUMAN_P, "s-missing")
    assert exc3.value.code == "TASK_NOT_FOUND"


def test_projection_rebuild_reproduces_identical_snapshot(engine: Engine) -> None:  # V-P1-10
    # self-contained: create Tasks in several states so the projection is non-trivial on its own
    done = run(
        engine, tk.CreateTask("r-done", str(CHANNEL), "research"), HUMAN_P, "r-done"
    ).resource_id
    _drive_to_verifying(engine, done, "r-done", "vr-r-done")
    run(engine, tk.RecordVerificationResult(done, "vr-r-done", "PASSED"), VERIFIER_P, "r-done-pass")
    run(engine, tk.CompleteTask(done, "doc-r"), HUMAN_P, "r-done-complete")
    gone = run(
        engine, tk.CreateTask("r-gone", str(CHANNEL), "research"), HUMAN_P, "r-gone"
    ).resource_id
    run(engine, tk.CancelTask(gone, "NOPE"), HUMAN_P, "r-gone-cancel")
    open_task = run(
        engine, tk.CreateTask("r-open", str(CHANNEL), "research"), HUMAN_P, "r-open"
    ).resource_id
    run(engine, tk.DelegateTask(open_task, "acct-t-agent"), HUMAN_P, "r-open-delegate")
    with Session(engine) as s, s.begin():
        before_rows = [
            dict(r)
            for r in s.execute(text("SELECT * FROM tasks_projection ORDER BY task_id")).mappings()
        ]
        before = snapshot_hash(s, "tasks")
        s.execute(text("DELETE FROM tasks_projection"))
        assert s.execute(text("SELECT count(*) FROM tasks_projection")).scalar_one() == 0
        after = rebuild(s, "tasks")
        after_rows = [
            dict(r)
            for r in s.execute(text("SELECT * FROM tasks_projection ORDER BY task_id")).mappings()
        ]
        checkpoint = s.execute(
            text(
                "SELECT last_recorded_seq, snapshot_hash FROM "
                "projection_checkpoints WHERE projection = 'tasks'"
            )
        ).first()
    assert after == before, "rebuild must reproduce the identical canonical snapshot hash"
    assert after_rows == before_rows and len(after_rows) >= 3
    assert checkpoint is not None and checkpoint[1] == after and checkpoint[0] > 0
    assert {r["status"] for r in after_rows} >= {"COMPLETED", "CANCELLED", "DELEGATED"}
    assert all(r["status"] in TaskStatus.__members__ for r in after_rows)
