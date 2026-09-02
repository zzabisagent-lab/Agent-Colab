"""P3-08 Limits enforcement (V-P3-15): requests beyond concurrent Task / rate limits are rejected
server-side with an audit entry and zero side effects; requests within limits proceed."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.agents import limits as lim
from server.agents import registry as reg
from server.api.dispatch import Runtime, execute_command
from server.api.errors import ApiError
from server.application import agents as ag
from server.application import tasks as t
from server.application import work as w
from server.application.authz import BusAuthorizer
from server.application.bus import CommandError
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import FixedClock
from server.identity.principals import Principal
from server.policy.repository import PostgresPolicyRepository
from server.usage.versions import activate_from_file

pytestmark = pytest.mark.db
WS, ADMIN, CHANNEL = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
T0 = dt.datetime(2026, 9, 2, 8, 0, tzinfo=dt.UTC)
ADMIN_P = Principal("acct-lim-admin", str(ADMIN), "human", "sha256:acct-lim-admin")
CRITERIA = ({"statement": "done", "check_type": "evidence", "required": True},)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-lim', 'lim')"),
            {"i": WS},
        )
        s.execute(
            text(
                "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
                "VALUES (:i, 'acct-lim-admin', :w, 'human', 'Admin')"
            ),
            {"i": ADMIN, "w": WS},
        )
        s.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name) "
                "VALUES (:i, 'chan-lim', :w, 'work', 'lim')"
            ),
            {"i": CHANNEL, "w": WS},
        )
        s.execute(
            text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
            {"c": CHANNEL, "a": ADMIN},
        )
        repo = PostgresPolicyRepository()
        repo.create_role(s, WS, "role-lim-admin", "admin")
        repo.commit_role_version(s, "role-lim-admin", ["agent.manage", "task.*"], [], {}, ADMIN)
        repo.assign_role(s, ADMIN, "role-lim-admin", ADMIN, T0)
        repo.create_role(s, WS, "role-lim-worker", "worker")
        repo.commit_role_version(
            s, "role-lim-worker", ["agent.self", "task.*", "work.poll"], [], {}, ADMIN
        )
    with Session(eng) as s, s.begin():
        activate_from_file(s)  # §7C usage recording needs an active pricing version
    yield eng
    eng.dispose()


def _rt(engine: Engine, clock: FixedClock) -> Runtime:
    return Runtime(make_session_factory(engine), BusAuthorizer(), None, clock, str(WS))


def _run(rt: Runtime, principal: Principal, cmd: Any, key: str) -> Any:
    return execute_command(rt, principal, cmd, idempotency_key=key, correlation_id="corr-lim")


def _events(engine: Engine) -> int:
    with Session(engine) as s:
        return int(
            s.execute(
                text("SELECT count(*) FROM events WHERE workspace_id = :w"), {"w": WS}
            ).scalar_one()
        )


def _limit_audits(engine: Engine) -> int:
    with Session(engine) as s:
        return int(
            s.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE action = "
                    "'agent.limit_exceeded' AND workspace_id = :w"
                ),
                {"w": WS},
            ).scalar_one()
        )


def _setup_agent(engine: Engine, clock: FixedClock) -> tuple[Runtime, Principal]:
    rt = _rt(engine, clock)
    _run(
        rt,
        ADMIN_P,
        ag.RegisterAgent(
            "agent-lim-1",
            "Limited",
            "mcp",
            roles=("role-lim-worker",),
            channel_ids=("chan-lim",),
            limits={"requests_per_minute": 2, "concurrent_tasks": 1},
        ),
        "lim-reg",
    )
    _run(
        rt,
        ADMIN_P,
        ag.ActivateAgent("agent-lim-1", probe={"identity_hash": "id-lim", "capabilities": []}),
        "lim-act",
    )
    with Session(engine) as s:
        row = reg.load_agent(s, WS, "agent-lim-1")
        assert row is not None
    return rt, Principal(
        row.account_public_id, str(row.account_id), "agent", "sha256:acct-agent-lim-1"
    )


def test_rate_limit_rejects_third_request_per_minute_with_audit(engine: Engine) -> None:
    clock = FixedClock(T0)
    rt, agent = _setup_agent(engine, clock)
    for i in range(2):  # within limits: processed normally
        res = _run(rt, agent, w.WorkPoll("agent-lim-1", max_items=1), f"poll-{i}")
        assert res.data["items"] == []
    events_before, audits_before = _events(engine), _limit_audits(engine)
    with pytest.raises(ApiError) as exc:
        _run(rt, agent, w.WorkPoll("agent-lim-1", max_items=1), "poll-3")
    assert exc.value.code == "AGENT_LIMIT_EXCEEDED" and exc.value.status == 429
    assert exc.value.extra["limit"] == "requests_per_minute" and exc.value.extra["configured"] == 2
    assert _events(engine) == events_before  # zero side effects
    assert _limit_audits(engine) == audits_before + 1  # audited despite the rollback
    with Session(engine) as s:
        assert lim.rate_window_count(s, "agent-lim-1", clock.now()) == 2
    clock.advance(dt.timedelta(minutes=1))  # a new window: processed normally again
    assert _run(rt, agent, w.WorkPoll("agent-lim-1", max_items=1), "poll-4").data["items"] == []


def test_concurrent_task_limit_rejects_second_acceptance(engine: Engine) -> None:
    clock = FixedClock(T0 + dt.timedelta(hours=1))
    rt = _rt(engine, clock)
    with Session(engine) as s:
        row = reg.load_agent(s, WS, "agent-lim-1")
        assert row is not None
    agent = Principal(
        row.account_public_id, str(row.account_id), "agent", "sha256:acct-agent-lim-1"
    )
    tasks = []
    for i in range(2):
        created = _run(
            rt,
            ADMIN_P,
            t.CreateTask(f"lim task {i}", str(CHANNEL), "research", "LOW", criteria=CRITERIA),
            f"lim-create-{i}",
        )
        tasks.append(created.resource_id)
        _run(
            rt,
            ADMIN_P,
            t.DelegateTask(created.resource_id, row.account_public_id),
            f"lim-delegate-{i}",
        )
        clock.advance(dt.timedelta(seconds=1))  # stay under the per-minute request limit
    clock.advance(dt.timedelta(minutes=1))
    first = _run(rt, agent, t.AcceptTask(tasks[0]), "lim-accept-0")
    assert first.data["status"] == "ACCEPTED"
    events_before, audits_before = _events(engine), _limit_audits(engine)
    with pytest.raises(ApiError) as exc:
        _run(rt, agent, t.AcceptTask(tasks[1]), "lim-accept-1")
    assert (
        exc.value.code == "AGENT_LIMIT_EXCEEDED" and exc.value.extra["limit"] == "concurrent_tasks"
    )
    assert _events(engine) == events_before and _limit_audits(engine) == audits_before + 1
    with Session(engine) as s:
        assert (
            s.execute(
                text("SELECT status FROM tasks_projection WHERE task_id = :t"), {"t": tasks[1]}
            ).scalar_one()
            == "DELEGATED"
        )
        # an accepted+re-accepted Task never double counts; the limit view exposes counters
        view = lim.limits_view(s, reg.load_agent(s, WS, "agent-lim-1"), clock.now())  # type: ignore[arg-type]
    assert view["current"]["concurrent_tasks"] == 1
    replay = _run(rt, agent, t.AcceptTask(tasks[0]), "lim-accept-0")
    assert replay.replayed
    # raising the limit lets the second acceptance through
    _run(
        rt,
        ADMIN_P,
        ag.UpdateAgent("agent-lim-1", limits={"requests_per_minute": 10, "concurrent_tasks": 2}),
        "lim-raise",
    )
    assert _run(rt, agent, t.AcceptTask(tasks[1]), "lim-accept-1b").data["status"] == "ACCEPTED"


def test_unknown_limit_kind_and_agent(engine: Engine) -> None:
    clock = FixedClock(T0)
    with Session(engine) as s:
        with pytest.raises(CommandError) as exc:
            lim.enforce_limits(s, "agent-lim-1", "teleport", clock, workspace_id=WS)
        assert exc.value.code == "AGENT_LIMIT_KIND_INVALID"
        with pytest.raises(CommandError) as exc2:
            lim.enforce_limits(s, "agent-nope", "request", clock, workspace_id=WS)
        assert exc2.value.code == "AGENT_NOT_FOUND"
