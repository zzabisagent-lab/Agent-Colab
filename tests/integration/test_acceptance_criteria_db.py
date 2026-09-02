"""V-P1-28: delegate without criteria, submit without required evidence, and criteria changes
only through a new revision + Event (P1-11)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from server.application import criteria as crit_app
from server.application import tasks as tk
from server.application.bus import CommandContext, CommandError, CommandResult, Principal, execute
from server.db.engine import make_engine
from server.domain.clock import SteppingClock
from server.domain.criteria import criteria_id
from server.events.postgres_store import PostgresEventStore

pytestmark = pytest.mark.db

WS = uuid.uuid4()
CHANNEL = uuid.uuid4()
HUMAN = uuid.uuid4()
AGENT = uuid.uuid4()
CLOCK = SteppingClock(dt.datetime(2026, 4, 1, tzinfo=dt.UTC))
CRITERIA: tuple[dict[str, Any], ...] = (
    {"statement": "report.md attached as an Artifact", "check_type": "artifact_hash"},
    {"statement": "screenshot attached", "check_type": "evidence", "required": False},
)


class AllowAll:
    def require(
        self, session: Session, principal_account_id: str, permission: str, **_: Any
    ) -> None:
        return None


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-crit', 'crit')"),
            {"i": WS},
        )
        for acc, name, typ in ((HUMAN, "acct-c-human", "human"), (AGENT, "acct-c-agent", "agent")):
            c.execute(
                text(
                    "INSERT INTO accounts "
                    "(id, account_id, workspace_id, account_type, display_name) "
                    "VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc, "a": name, "w": WS, "t": typ},
            )
        c.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name) "
                "VALUES (:i, 'chan-crit', :w, 'work', 'crit')"
            ),
            {"i": CHANNEL, "w": WS},
        )
    yield eng
    eng.dispose()


HUMAN_P = Principal("acct-c-human", str(HUMAN), "human", "fp-h")
AGENT_P = Principal("acct-c-agent", str(AGENT), "agent", "fp-a")


def run(engine: Engine, cmd: Any, who: Principal, key: str) -> CommandResult:
    with Session(engine) as s, s.begin():
        ctx = CommandContext(
            session=s,
            store=PostgresEventStore(s, clock=CLOCK),
            authorizer=AllowAll(),
            clock=CLOCK,
            principal=who,
            workspace_id=str(WS),
            correlation_id="corr-crit",
            idempotency_key=key,
        )
        return execute(cmd, ctx)


def _events(engine: Engine, task_id: str) -> list[tuple[str, str]]:
    with Session(engine) as s:
        return [
            (str(r[0]), str(r[1]))
            for r in s.execute(
                text(
                    "SELECT aggregate_type, type FROM events WHERE task_id = :t "
                    "ORDER BY recorded_seq"
                ),
                {"t": task_id},
            )
        ]


def _rows(engine: Engine, task_id: str) -> list[tuple[str, int, str, str, bool]]:
    with Session(engine) as s:
        return [
            (str(r[0]), int(r[1]), str(r[2]), str(r[3]), bool(r[4]))
            for r in s.execute(
                text(
                    "SELECT criteria_id, revision, statement, check_type, required "
                    "FROM task_acceptance_criteria WHERE task_id = :t "
                    "ORDER BY revision, criteria_id"
                ),
                {"t": task_id},
            )
        ]


def test_delegate_without_criteria_is_rejected_with_zero_side_effects(engine: Engine) -> None:
    created = run(
        engine, tk.CreateTask("No criteria", str(CHANNEL), "research"), HUMAN_P, "nc-create"
    )
    tid = created.resource_id
    assert _rows(engine, tid) == []
    before = _events(engine, tid)
    with pytest.raises(CommandError) as exc:
        run(engine, tk.DelegateTask(tid, "acct-c-agent"), HUMAN_P, "nc-delegate")
    assert exc.value.code == "ACCEPTANCE_CRITERIA_REQUIRED"
    assert _events(engine, tid) == before == [("task", "TASK_CREATED")]
    with Session(engine) as s:
        assert (
            s.execute(
                text("SELECT status FROM tasks_projection WHERE task_id = :t"), {"t": tid}
            ).scalar()
            == "OPEN"
        )
        assert (
            s.execute(
                text("SELECT count(*) FROM task_assignments WHERE task_id = :t"), {"t": tid}
            ).scalar_one()
            == 0
        )
    # a revision pins criteria; delegation then succeeds
    rev = run(engine, crit_app.ReviseCriteria(tid, CRITERIA), HUMAN_P, "nc-revise")
    assert rev.data["criteria_revision"] == 1 and rev.aggregate_type == "task_criteria"
    run(engine, tk.DelegateTask(tid, "acct-c-agent"), HUMAN_P, "nc-delegate-2")
    assert _events(engine, tid)[-1] == ("task", "TASK_DELEGATED")


def test_invalid_creation_criteria_reject_before_any_event(engine: Engine) -> None:
    tid = "task-crit-invalid"
    with pytest.raises(CommandError) as exc:
        run(
            engine,
            tk.CreateTask(
                "Bad",
                str(CHANNEL),
                "research",
                task_id=tid,
                criteria=({"statement": "", "check_type": "evidence"},),
            ),
            HUMAN_P,
            "bad-create",
        )
    assert exc.value.code == "ACCEPTANCE_CRITERIA_INVALID"
    assert _events(engine, tid) == []


def test_submit_requires_evidence_for_required_criteria_then_pins_revision(engine: Engine) -> None:
    created = run(
        engine,
        tk.CreateTask("With criteria", str(CHANNEL), "research", criteria=CRITERIA),
        HUMAN_P,
        "wc-create",
    )
    tid = created.resource_id
    rows = _rows(engine, tid)
    assert [(r[1], r[3], r[4]) for r in rows] == [
        (1, "artifact_hash", True),
        (1, "evidence", False),
    ] or [(r[1], r[3], r[4]) for r in rows] == [(1, "evidence", False), (1, "artifact_hash", True)]
    required_id = criteria_id(tid, 1, 0, CRITERIA[0]["statement"])
    optional_id = criteria_id(tid, 1, 1, CRITERIA[1]["statement"])
    assert {r[0] for r in rows} == {required_id, optional_id}
    with Session(engine) as s:  # pinned in the TASK_CREATED payload
        payload = s.execute(
            text("SELECT payload FROM events WHERE task_id = :t AND type = 'TASK_CREATED'"),
            {"t": tid},
        ).scalar_one()
    assert [c["criteria_id"] for c in payload["criteria"]] == [required_id, optional_id]
    run(engine, tk.DelegateTask(tid, "acct-c-agent"), HUMAN_P, "wc-delegate")
    run(engine, tk.AcceptTask(tid), AGENT_P, "wc-accept")
    run(engine, tk.StartTask(tid), AGENT_P, "wc-start")
    before = _events(engine, tid)
    with pytest.raises(CommandError) as exc:  # evidence only for the optional criterion
        run(
            engine,
            tk.SubmitImplementation(tid, (f"{optional_id}:art-2", "art-general"), 1),
            AGENT_P,
            "wc-submit-1",
        )
    assert exc.value.code == "EVIDENCE_REQUIRED" and exc.value.extra["missing"] == [required_id]
    with pytest.raises(CommandError) as exc2:  # stale revision number is rejected too
        run(
            engine,
            tk.SubmitImplementation(tid, (f"{required_id}:sha256:abc",), 0),
            AGENT_P,
            "wc-submit-0",
        )
    assert exc2.value.code == "CRITERIA_REVISION_STALE"
    assert _events(engine, tid) == before
    with Session(engine) as s:
        assert (
            s.execute(
                text("SELECT status FROM tasks_projection WHERE task_id = :t"), {"t": tid}
            ).scalar()
            == "RUNNING"
        )
    ok = run(
        engine,
        tk.SubmitImplementation(tid, (f"{required_id}:sha256:abc",), 1),
        AGENT_P,
        "wc-submit-2",
    )
    assert ok.data["status"] == "IMPLEMENTED"
    with Session(engine) as s:
        assert s.execute(
            text("SELECT status, criteria_revision FROM tasks_projection WHERE task_id = :t"),
            {"t": tid},
        ).first() == ("IMPLEMENTED", 1)


def test_criteria_change_only_via_new_revision_and_event(engine: Engine) -> None:
    created = run(
        engine,
        tk.CreateTask("Revisable", str(CHANNEL), "research", criteria=CRITERIA),
        HUMAN_P,
        "rv-create",
    )
    tid = created.resource_id
    rows_v1 = _rows(engine, tid)
    new = ({"statement": "tests pass", "check_type": "test_command"},)
    rev = run(engine, crit_app.ReviseCriteria(tid, new), HUMAN_P, "rv-revise")
    assert rev.data["criteria_revision"] == 2 and rev.aggregate_seq == 1
    again = run(engine, crit_app.ReviseCriteria(tid, new), HUMAN_P, "rv-revise")  # idempotent
    assert again.replayed and again.event_id == rev.event_id
    events = _events(engine, tid)
    assert events.count(("task_criteria", "ACCEPTANCE_CRITERIA_REVISED")) == 1
    rows = _rows(engine, tid)
    assert [r for r in rows if r[1] == 1] == rows_v1  # revision 1 rows untouched
    assert [(r[1], r[2]) for r in rows if r[1] == 2] == [(2, "tests pass")]
    current = crit_app.current_criteria(Session(engine), tid)
    assert current.revision == 2 and [c.statement for c in current.criteria] == ["tests pass"]
    with Session(engine) as s:
        ev = s.execute(
            text("SELECT payload, aggregate_id FROM events WHERE event_id = :e"),
            {"e": rev.event_id},
        ).first()
        assert (
            ev is not None
            and ev[1] == tid
            and ev[0]["criteria_ids"] == [criteria_id(tid, 2, 0, "tests pass")]
        )
    with pytest.raises(DBAPIError, match="IMMUTABLE_ROW"), engine.begin() as c:
        c.execute(
            text("UPDATE task_acceptance_criteria SET statement = 'forged' WHERE task_id = :t"),
            {"t": tid},
        )
    with pytest.raises(CommandError) as exc:
        run(engine, crit_app.ReviseCriteria(tid, ()), HUMAN_P, "rv-empty")
    assert exc.value.code == "ACCEPTANCE_CRITERIA_REQUIRED"
    assert _events(engine, tid) == events
    # submit must reference the current (latest) revision
    run(engine, tk.DelegateTask(tid, "acct-c-agent"), HUMAN_P, "rv-delegate")
    run(engine, tk.AcceptTask(tid), AGENT_P, "rv-accept")
    run(engine, tk.StartTask(tid), AGENT_P, "rv-start")
    with pytest.raises(CommandError) as exc3:
        run(
            engine,
            tk.SubmitImplementation(tid, (f"{rows_v1[0][0]}:art-1",), 1),
            AGENT_P,
            "rv-submit-old",
        )
    assert exc3.value.code == "CRITERIA_REVISION_STALE"
    ok = run(
        engine,
        tk.SubmitImplementation(tid, (f"{criteria_id(tid, 2, 0, 'tests pass')}:junit.xml",), 2),
        AGENT_P,
        "rv-submit",
    )
    assert ok.data["status"] == "IMPLEMENTED"
