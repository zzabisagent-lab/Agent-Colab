"""Schedule Run execution: concurrency, policy re-check, Agent selection, Secret leases and
Approvals (P5-04/P5-05; V-P5-09, V-P5-10, V-P5-11, V-P5-15, V-P5-16, V-P5-17, V-P5-18, V-P5-30)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.schedules import execution
from server.schedules.contract import RunStatus, SkipCode
from tests.integration.schedule_exec_fixture import (
    CAPABILITY_SELECTION,
    FIXED_AGENT_SELECTION,
    Fixture,
    advance,
)
from tests.integration.schedule_seed import T0

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture
def fx(engine: Engine) -> Fixture:
    return Fixture.create(engine, f"exec{uuid.uuid4().hex[:6]}", FixedClock(T0))


def test_forbid_skips_while_a_previous_run_is_active(fx: Fixture, engine: Engine) -> None:
    """V-P5-09: FORBID → the new Run is SKIPPED with SKIPPED_CONCURRENCY and creates no Task."""
    with Session(engine) as s, s.begin():
        fx.schedule(s, "sch-forbid", concurrency="FORBID")
        fx.run(s, "sch-forbid", run_id="run-forbid-old", status="RUNNING", task_id="task-old")
        new = fx.run(s, "sch-forbid", run_id="run-forbid-new")
        before = len(fx.tasks(s))
        outcome = execution.execute(new, fx.ctx(s))
        assert outcome.status == RunStatus.SKIPPED.value
        assert outcome.error_code == SkipCode.SKIPPED_CONCURRENCY.value
        assert len(fx.tasks(s)) == before  # zero side effects
        assert "RUN_SKIPPED" in fx.run_events(s, "run-forbid-new")


def test_allow_runs_both_independently(fx: Fixture, engine: Engine) -> None:
    """V-P5-10: ALLOW → the second Run executes and tracks its own Task."""
    with Session(engine) as s, s.begin():
        fx.schedule(s, "sch-allow", concurrency="ALLOW")
        fx.run(s, "sch-allow", run_id="run-allow-old", status="RUNNING", task_id="task-old-allow")
        new = fx.run(s, "sch-allow", run_id="run-allow-new")
        outcome = execution.execute(new, fx.ctx(s))
        assert outcome.status == RunStatus.TASK_CREATED.value and outcome.task_id
        assert fx.reload(s, "run-allow-old").status == RunStatus.RUNNING.value
        assert "RUN_STARTED" in fx.run_events(s, "run-allow-new")


def test_replace_cancels_then_starts_and_times_out_when_unconfirmed(
    fx: Fixture, engine: Engine
) -> None:
    """V-P5-11: REPLACE cancels the previous Run first; an unresponsive cancel skips the new Run
    with SKIPPED_REPLACE_CANCEL_TIMEOUT after 60 s instead of running concurrently."""
    with Session(engine) as s, s.begin():
        fx.schedule(s, "sch-replace", concurrency="REPLACE")
        fx.run(s, "sch-replace", run_id="run-rep-old", status="RUNNING", task_id="task-rep-old")
        new = fx.run(s, "sch-replace", run_id="run-rep-new")
        first = execution.execute(new, fx.ctx(s))
        assert first.deferred and fx.reload(s, "run-rep-old").status == "CANCEL_REQUESTED"
        # the previous Run confirms its cleanup: the new Run starts
        ctx = fx.ctx(s)
        execution.finish_cancel(
            ctx, fx.reload(s, "run-rep-old"), timed_out=False, reason="CANCELLED"
        )
        advance(fx.clock, 5)
        second = execution.execute(fx.reload(s, "run-rep-new"), fx.ctx(s))
        assert second.status == RunStatus.TASK_CREATED.value

    with Session(engine) as s, s.begin():
        fx.schedule(s, "sch-replace2", concurrency="REPLACE")
        fx.run(s, "sch-replace2", run_id="run-rep2-old", status="RUNNING", task_id="task-rep2-old")
        blocked = fx.run(s, "sch-replace2", run_id="run-rep2-new")
        assert execution.execute(blocked, fx.ctx(s)).deferred
        advance(fx.clock, 61)  # the previous Run never confirms
        outcome = execution.execute(fx.reload(s, "run-rep2-new"), fx.ctx(s))
        assert outcome.status == RunStatus.SKIPPED.value
        assert outcome.error_code == SkipCode.SKIPPED_REPLACE_CANCEL_TIMEOUT.value


def test_revoked_principal_skips_with_policy_and_zero_tasks(fx: Fixture, engine: Engine) -> None:
    """V-P5-15: the execution principal lost its permission/status → SKIPPED_POLICY, zero Tasks."""
    with Session(engine) as s, s.begin():
        fx.schedule(s, "sch-policy")
        run = fx.run(s, "sch-policy", run_id="run-policy-1")
        s.execute(
            text("UPDATE accounts SET status = 'SUSPENDED' WHERE id = :a"),
            {"a": fx.seed.accounts[fx.seed.owner]},
        )
        before = len(fx.tasks(s))
        outcome = execution.execute(run, fx.ctx(s))
        assert outcome.status == RunStatus.SKIPPED.value
        assert outcome.error_code == SkipCode.SKIPPED_POLICY.value
        assert len(fx.tasks(s)) == before


def test_suspended_fixed_agent_skips_unless_a_capability_fallback_exists(
    fx: Fixture, engine: Engine
) -> None:
    """V-P5-16: a fixed Agent that is suspended skips with SKIPPED_AGENT_UNAVAILABLE; with
    ``fallback`` the capability query selects only an eligible substitute."""
    with Session(engine) as s, s.begin():
        fx.schedule(s, "sch-agent", agent_selection=dict(FIXED_AGENT_SELECTION))
        run = fx.run(s, "sch-agent", run_id="run-agent-1")
        s.execute(
            text("UPDATE agents SET status = 'suspended' WHERE agent_id = :g"),
            {"g": fx.seed.agent_id},
        )
        outcome = execution.execute(run, fx.ctx(s))
        assert outcome.error_code == SkipCode.SKIPPED_AGENT_UNAVAILABLE.value

    with Session(engine) as s, s.begin():
        # the only Agent is still suspended: the capability query finds nobody eligible
        fx.schedule(s, "sch-agent2", agent_selection=dict(CAPABILITY_SELECTION))
        run = fx.run(s, "sch-agent2", run_id="run-agent-2")
        assert execution.execute(run, fx.ctx(s)).error_code == (
            SkipCode.SKIPPED_AGENT_UNAVAILABLE.value
        )
        s.execute(
            text("UPDATE agents SET status = 'active', online = true WHERE agent_id = :g"),
            {"g": fx.seed.agent_id},
        )
        run3 = fx.run(s, "sch-agent2", run_id="run-agent-3")
        outcome = execution.execute(run3, fx.ctx(s))
        assert outcome.status == RunStatus.TASK_CREATED.value


def test_secret_reference_needs_a_grant_and_leases_are_short_lived(
    fx: Fixture, engine: Engine
) -> None:
    """V-P5-17: a template Secret reference without a grant skips; with a grant the Run gets a
    short single-use lease that is revoked when the Run ends. No value is ever stored."""
    template = {
        "schema_id": "action-template.v1",
        "action": "task_create",
        "input": {"title": "with secret", "domain": "research"},
        "secret_refs": ["secret://ops/api-key"],
    }
    with Session(engine) as s, s.begin():
        fx.schedule(
            s, "sch-secret", agent_selection=dict(CAPABILITY_SELECTION), action_template=template
        )
        run = fx.run(s, "sch-secret", run_id="run-secret-1")
        outcome = execution.execute(run, fx.ctx(s))
        assert outcome.error_code == SkipCode.SKIPPED_POLICY.value
        assert "SECRET_GRANT_MISSING" in outcome.detail

    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "INSERT INTO secrets (secret_ref, workspace_id, name, provider, current_version, "
                "metadata, created_by, created_at) VALUES ('secret://ops/api-key', :w, 'api-key', "
                "'local', 1, CAST('{}' AS jsonb), :by, :now) ON CONFLICT DO NOTHING"
            ),
            {"w": fx.seed.ws, "by": fx.seed.accounts[fx.seed.owner], "now": fx.clock.now()},
        )
        s.execute(
            text(
                "INSERT INTO secret_grants (grant_id, workspace_id, secret_ref, agent_id, "
                "task_id, action, ttl_seconds, single_use, expires_at, created_by, "
                "created_at) VALUES (:g, :w, 'secret://ops/api-key', :a, NULL, NULL, 300, true, "
                ":exp, :by, :now)"
            ),
            {
                "g": f"grt-{uuid.uuid4().hex[:12]}",
                "w": fx.seed.ws,
                "a": fx.seed.agent_id,
                "exp": fx.clock.now() + dt.timedelta(hours=1),
                "by": fx.seed.accounts[fx.seed.owner],
                "now": fx.clock.now(),
            },
        )
        run = fx.run(s, "sch-secret", run_id="run-secret-2")
        outcome = execution.execute(run, fx.ctx(s))
        assert outcome.status == RunStatus.TASK_CREATED.value
        leases = s.execute(
            text("SELECT expires_at, single_use, revoked_at FROM secret_leases WHERE task_id = :t"),
            {"t": outcome.task_id},
        ).all()
        assert leases and leases[0][1] is True
        assert (leases[0][0] - fx.clock.now()).total_seconds() <= 300
        # the Run ends: leases are revoked with the Task (§9.3)
        fx.finish_task(s, str(outcome.task_id), "COMPLETED")
        execution.on_task_terminal(fx.ctx(s), str(outcome.task_id), "COMPLETED")
        revoked = s.execute(
            text("SELECT revoked_at FROM secret_leases WHERE task_id = :t"),
            {"t": outcome.task_id},
        ).first()
        assert revoked is not None and revoked[0] is not None


def _approval(
    s: Session,
    fx: Fixture,
    subject_type: str,
    subject_id: str,
    *,
    max_uses: int | None,
    status: str = "APPROVED",
) -> str:
    approval_id = f"apr-{uuid.uuid4().hex[:12]}"
    s.execute(
        text(
            "INSERT INTO approval_grants (id, approval_id, workspace_id, subject_type, subject_id, "
            "action, risk, status, requested_by, valid_from, expires_at, max_uses, "
            "quorum_required, aggregate_seq) VALUES (:i, :a, :w, :st, :sid, 'api:schedule_run', "
            "'HIGH', :status, :by, :from, :to, :mu, 1, 0)"
        ),
        {
            "i": uuid.uuid4(),
            "a": approval_id,
            "w": fx.seed.ws,
            "st": subject_type,
            "sid": subject_id,
            "status": status,
            "by": fx.seed.accounts[fx.seed.owner],
            "from": fx.clock.now() - dt.timedelta(minutes=1),
            "to": fx.clock.now() + dt.timedelta(hours=1),
            "mu": max_uses,
        },
    )
    return approval_id


def test_approval_is_required_and_consumed_atomically(fx: Fixture, engine: Engine) -> None:
    """V-P5-18: a Run needing approval does not execute without one; a bounded Schedule Approval
    is consumed with the Task and refuses execution once exhausted."""
    template = {
        "schema_id": "action-template.v1",
        "action": "task_create",
        "input": {"title": "needs approval", "domain": "research", "requires_approval": True},
    }
    with Session(engine) as s, s.begin():
        fx.schedule(s, "sch-appr", action_template=template)
        run = fx.run(s, "sch-appr", run_id="run-appr-1")
        outcome = execution.execute(run, fx.ctx(s))
        assert outcome.error_code == SkipCode.SKIPPED_POLICY.value
        assert "APPROVAL_REQUIRED" in outcome.detail

    with Session(engine) as s, s.begin():
        _approval(s, fx, "schedule", "sch-appr", max_uses=1)
        run = fx.run(s, "sch-appr", run_id="run-appr-2")
        assert execution.execute(run, fx.ctx(s)).status == RunStatus.TASK_CREATED.value
        used = s.execute(
            text("SELECT count(*) FROM approval_consumptions WHERE consumed_for_id = 'sch-appr'")
        ).scalar_one()
        assert int(used) == 1
        run = fx.run(s, "sch-appr", run_id="run-appr-3")
        exhausted = execution.execute(run, fx.ctx(s))
        assert exhausted.error_code == SkipCode.SKIPPED_POLICY.value  # max_uses reached


def test_two_runners_consume_a_single_use_approval_once(fx: Fixture, engine: Engine) -> None:
    """V-P5-30: two runners race on a max-use=1 Schedule Approval → 1 consumption, 1 execution."""
    template = {
        "schema_id": "action-template.v1",
        "action": "task_create",
        "input": {"title": "race", "domain": "research", "requires_approval": True},
    }
    with Session(engine) as s, s.begin():
        fx.schedule(s, "sch-race", concurrency="ALLOW", action_template=template)
        _approval(s, fx, "schedule", "sch-race", max_uses=1)
        fx.run(s, "sch-race", run_id="run-race-a")
        fx.run(s, "sch-race", run_id="run-race-b")

    factory = fx.runtime().session_factory
    outcomes = []
    for runner, run_id in (("runner-a", "run-race-a"), ("runner-b", "run-race-b")):
        # each runner works in its own transaction; the grant row lock serializes the consumption
        session = factory()
        try:
            session.begin()
            ctx = fx.ctx(session, runner_id=runner)
            outcomes.append(execution.execute(ctx.store.load_run(session, run_id), ctx))
            session.commit()
        finally:
            session.close()

    started = [o for o in outcomes if o.task_id]
    skipped = [o for o in outcomes if o.status == RunStatus.SKIPPED.value]
    assert len(started) == 1 and len(skipped) == 1
    with Session(engine) as s:
        used = s.execute(
            text("SELECT count(*) FROM approval_consumptions WHERE consumed_for_id = 'sch-race'")
        ).scalar_one()
        assert int(used) == 1
