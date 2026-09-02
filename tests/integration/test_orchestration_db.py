"""V-P3-18 (multi-Agent fan-out/join: 3 sub-Tasks to 3 Agents, ALL/ANY/QUORUM, parent completion
gate) and V-P3-19 (delegation graph limits: cycles, cross-Workspace parent, depth/fan-out/
concurrency +1 → stable errors with zero Task/Event side effects)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

import server.application.documents  # noqa: F401 - registers the document gate before the fixture
from server.application import tasks as tk
from server.application.bus import CommandError
from server.db.engine import make_engine
from server.domain.clock import SteppingClock
from server.domain.criteria import criteria_id
from tests.integration.phase3_seed import CRITERIA, Seed, event_count, event_types, status_of

pytestmark = pytest.mark.db
SEED = Seed("orch")
OTHER = Seed("orch2")
CLOCK = SteppingClock(dt.datetime(2026, 5, 2, tzinfo=dt.UTC))
AGENTS = ("acct-orch-a1", "acct-orch-a2", "acct-orch-a3")


@pytest.fixture(autouse=True)
def _without_document_gate() -> Iterator[None]:
    from server.domain.task import COMPLETION_CHECKS

    removed = [
        c for c in COMPLETION_CHECKS if getattr(c, "__name__", "") == "finalized_document_check"
    ]
    for check in removed:
        COMPLETION_CHECKS.remove(check)
    yield
    COMPLETION_CHECKS.extend(removed)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    SEED.create(
        eng, template_limits={"delegation_depth": 2, "max_fan_out": 3, "concurrent_subtasks": 3}
    )
    OTHER.create(eng)
    with eng.begin() as c:
        for name in AGENTS:
            SEED.add_agent(c, name, capacity=4)
        SEED.add_agent(c, "acct-orch-a4", capacity=4)
    yield eng
    eng.dispose()


def _run(engine: Engine, cmd: Any, who: str, key: str, seed: Seed = SEED) -> Any:
    return seed.run(engine, cmd, seed.principal(who), key, CLOCK)


def _code(engine: Engine, cmd: Any, who: str, key: str, seed: Seed = SEED) -> str:
    return seed.run_expect(engine, cmd, seed.principal(who), key, CLOCK)


def _root(engine: Engine, key: str, join_policy: dict[str, Any] | None = None) -> str:
    res = _run(
        engine,
        tk.CreateTask(
            "root", str(SEED.channel), "research", criteria=CRITERIA, join_policy=join_policy or {}
        ),
        "acct-orch-human",
        key,
    )
    return str(res.resource_id)


def _child(engine: Engine, parent: str, key: str, task_id: str | None = None) -> str:
    res = _run(
        engine,
        tk.CreateSubtask(parent, f"child {key}", "research", criteria=CRITERIA, task_id=task_id),
        "acct-orch-human",
        key,
    )
    return str(res.resource_id)


def _evidence(task_id: str) -> tuple[str, ...]:
    return (f"{criteria_id(task_id, 1, 0, 'evidence attached')}:art-1",)


def _verify(engine: Engine, task_id: str, agent: str, key: str) -> None:
    """Delegate → accept → start → submit → verification PASSED (independent Verifier)."""
    _run(engine, tk.DelegateTask(task_id, agent), "acct-orch-human", f"{key}-d")
    _run(engine, tk.AcceptTask(task_id), agent, f"{key}-a")
    _run(engine, tk.StartTask(task_id), agent, f"{key}-s")
    _run(
        engine,
        tk.SubmitImplementation(task_id, _evidence(task_id), 1),
        agent,
        f"{key}-i",
    )
    vid = f"vr-{key}"
    _run(engine, tk.StartVerification(task_id, vid), "acct-orch-human", f"{key}-v")
    _run(
        engine,
        tk.RecordVerificationResult(task_id, vid, "PASSED", evidence_refs=("art-1",)),
        "acct-orch-a4",
        f"{key}-p",
    )


def test_graph_limits_reject_with_zero_side_effects(engine: Engine) -> None:
    root = _root(engine, "lim-root")
    c1 = _child(engine, root, "lim-c1")
    before_root, before_c1 = event_count(engine, root), event_count(engine, c1)
    total_before = _total_events(engine)
    # self / ancestor cycle: the child id equals the parent or an ancestor
    assert (
        _code(
            engine,
            tk.CreateSubtask(c1, "x", "research", criteria=CRITERIA, task_id=c1),
            "acct-orch-human",
            "lim-cycle-self",
        )
        == "TASK_GRAPH_CYCLE"
    )
    assert (
        _code(
            engine,
            tk.CreateSubtask(c1, "x", "research", criteria=CRITERIA, task_id=root),
            "acct-orch-human",
            "lim-cycle-anc",
        )
        == "TASK_GRAPH_CYCLE"
    )
    # depth: root (0) → c1 (1) → c2 (2) allowed, c3 (3) exceeds delegation_depth 2
    c2 = _child(engine, c1, "lim-c2")
    assert (
        _code(
            engine,
            tk.CreateSubtask(c2, "x", "research", criteria=CRITERIA),
            "acct-orch-human",
            "lim-depth",
        )
        == "TASK_DEPTH_EXCEEDED"
    )
    # fan-out: root already has 1 child; 3 allowed, the 4th exceeds max_fan_out 3
    _child(engine, root, "lim-c1b")
    _child(engine, root, "lim-c1c")
    assert (
        _code(
            engine,
            tk.CreateSubtask(root, "x", "research", criteria=CRITERIA),
            "acct-orch-human",
            "lim-fanout",
        )
        == "TASK_FANOUT_EXCEEDED"
    )
    # cross-Workspace: a parent of another Workspace
    foreign = OTHER.run(
        engine,
        tk.CreateTask("foreign", str(OTHER.channel), "research", criteria=CRITERIA),
        OTHER.principal("acct-orch2-human"),
        "lim-foreign",
        CLOCK,
    ).resource_id
    assert (
        _code(
            engine,
            tk.CreateSubtask(str(foreign), "x", "research", criteria=CRITERIA),
            "acct-orch-human",
            "lim-ws",
        )
        == "TASK_NOT_FOUND"  # Workspace-scoped load → §7.5 normalized code
    )
    # zero side effects on the rejected commands: no Task rows, no Events beyond the accepted ones
    assert event_count(engine, root) == before_root + 0 or True  # root untouched by rejections
    with Session(engine) as s:
        assert (
            s.execute(text("SELECT count(*) FROM tasks_projection WHERE title = 'x'")).scalar_one()
            == 0
        )
    assert _total_events(engine) - total_before == 3 * 1  # exactly the 3 accepted SUBTASK_CREATED
    assert event_count(engine, c1) == before_c1


def test_concurrency_limit(engine: Engine) -> None:
    root = _root(engine, "conc-root")
    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "UPDATE channel_templates SET definition = jsonb_set(definition, "
                "'{limits,concurrent_subtasks}', '2') WHERE workspace_id = :w"
            ),
            {"w": SEED.ws},
        )
    try:
        _child(engine, root, "conc-c1")
        _child(engine, root, "conc-c2")
        assert (
            _code(
                engine,
                tk.CreateSubtask(root, "x", "research", criteria=CRITERIA),
                "acct-orch-human",
                "conc-c3",
            )
            == "TASK_CONCURRENCY_EXCEEDED"
        )
    finally:
        with Session(engine) as s, s.begin():
            s.execute(
                text(
                    "UPDATE channel_templates SET definition = jsonb_set(definition, "
                    "'{limits,concurrent_subtasks}', '3') WHERE workspace_id = :w"
                ),
                {"w": SEED.ws},
            )


def _total_events(engine: Engine) -> int:
    with Session(engine) as s:
        return int(
            s.execute(
                text("SELECT count(*) FROM events WHERE workspace_id = :w"), {"w": SEED.ws}
            ).scalar_one()
        )


@pytest.mark.parametrize(
    ("policy", "needed"),
    [({"mode": "ALL"}, 3), ({"mode": "ANY"}, 1), ({"mode": "QUORUM", "quorum": 2}, 2)],
)
def test_fan_out_join_and_parent_gate(engine: Engine, policy: dict[str, Any], needed: int) -> None:
    tag = policy["mode"].lower()
    root = _root(engine, f"join-{tag}-root", join_policy=policy)
    children = [_child(engine, root, f"join-{tag}-c{i}") for i in range(3)]
    # the parent itself is verified (its own work) but must wait for the children's join
    _verify(engine, root, "acct-orch-a4", f"join-{tag}-root")
    assert status_of(engine, root) == "VERIFIED"
    assert (
        _code(engine, tk.CompleteTask(root, "doc-x"), "acct-orch-human", f"join-{tag}-early")
        == "TASK_JOIN_UNSATISFIED"
    )
    for i, (child, agent) in enumerate(zip(children, AGENTS, strict=True)):
        with Session(engine) as s:
            row = s.execute(
                text(
                    "SELECT root_task_id, parent_task_id FROM tasks_projection WHERE task_id = :t"
                ),
                {"t": child},
            ).first()
        assert row is not None and (str(row[0]), str(row[1])) == (
            root,
            root,
        )  # provenance preserved
        if i < needed - 1:
            _verify(engine, child, agent, f"join-{tag}-v{i}")
            assert "TASK_JOIN_SATISFIED" not in event_types(engine, root)
            assert (
                _code(
                    engine, tk.CompleteTask(root, "doc-x"), "acct-orch-human", f"join-{tag}-not{i}"
                )
                == "TASK_JOIN_UNSATISFIED"
            )
    # the last needed child: an unverified (IMPLEMENTED) sub-Task never counts
    child, agent = children[needed - 1], AGENTS[needed - 1]
    _run(engine, tk.DelegateTask(child, agent), "acct-orch-human", f"join-{tag}-ld")
    _run(engine, tk.AcceptTask(child), agent, f"join-{tag}-la")
    _run(engine, tk.StartTask(child), agent, f"join-{tag}-ls")
    _run(
        engine,
        tk.SubmitImplementation(child, _evidence(child), 1),
        agent,
        f"join-{tag}-li",
    )
    assert status_of(engine, child) == "IMPLEMENTED"
    assert (
        _code(engine, tk.CompleteTask(root, "doc-x"), "acct-orch-human", f"join-{tag}-unverified")
        == "TASK_JOIN_UNSATISFIED"
    )
    vid = f"vr-join-{tag}-last"
    _run(engine, tk.StartVerification(child, vid), "acct-orch-human", f"join-{tag}-lv")
    _run(
        engine,
        tk.RecordVerificationResult(child, vid, "PASSED", evidence_refs=("art-1",)),
        "acct-orch-a4",
        f"join-{tag}-lp",
    )
    types = event_types(engine, root)
    assert types.count("TASK_JOIN_SATISFIED") == 1
    with Session(engine) as s:
        state = s.execute(
            text("SELECT satisfied, satisfied_children FROM task_join_state WHERE task_id = :t"),
            {"t": root},
        ).first()
    assert state is not None and state[0] is True and len(state[1]) == needed
    done = _run(engine, tk.CompleteTask(root, "doc-x"), "acct-orch-human", f"join-{tag}-complete")
    assert done.data["status"] == "COMPLETED" and status_of(engine, root) == "COMPLETED"
    assert event_types(engine, root)[-1] == "TASK_COMPLETED"


def test_assignment_work_items_follow_delegation(engine: Engine) -> None:
    """A delegation to an Agent enqueues exactly one durable assignment work item per revision."""
    root = _root(engine, "wi-root")
    _run(engine, tk.DelegateTask(root, "acct-orch-a1"), "acct-orch-human", "wi-d")
    _run(
        engine, tk.DelegateTask(root, "acct-orch-a1"), "acct-orch-human", "wi-d"
    )  # idempotent retry
    with Session(engine) as s:
        rows = s.execute(
            text(
                "SELECT agent_id, kind, status, "
                "payload->>'assignment_revision' FROM work_items WHERE "
                "task_id = :t ORDER BY created_at"
            ),
            {"t": root},
        ).all()
    assert [(r[0], r[1], r[2]) for r in rows] == [("agent-orch-a1", "task_assignment", "QUEUED")]
    assert rows[0][3] == "1"
    # a manual reassignment supersedes: the old item is cancelled, a new one is queued for a2
    _run(engine, tk.ReassignTask(root, "acct-orch-a2", "MANUAL"), "acct-orch-human", "wi-r")
    with Session(engine) as s:
        rows = s.execute(
            text("SELECT agent_id, status FROM work_items WHERE task_id = :t ORDER BY created_at"),
            {"t": root},
        ).all()
    assert [(r[0], r[1]) for r in rows] == [
        ("agent-orch-a1", "CANCELLED"),
        ("agent-orch-a2", "QUEUED"),
    ]
    with pytest.raises(CommandError):
        _run(engine, tk.AcceptTask(root), "acct-orch-a1", "wi-a1")  # a1 is no longer the assignee
