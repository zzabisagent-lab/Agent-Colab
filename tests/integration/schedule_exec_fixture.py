"""Fixtures for the Phase 5 execution tests (P5-04..P5-07, P5-10).

Builds on the core package's ``schedule_seed`` (workspace, channel, accounts, Agent) and inserts
Schedules, pinned ScheduleVersions and Runs directly, so each execution behaviour can be driven
from an exact starting state with a virtual clock.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime
from server.application.authz import AllowAllAuthorizer
from server.db.engine import make_session_factory
from server.domain.clock import Clock, FixedClock
from server.events.postgres_store import PostgresEventStore
from server.schedules import execution
from server.schedules.execution import ExecutionContext, RunLike
from server.schedules.run_access import DbRunStore
from tests.integration.schedule_seed import T0, Seed

ACTION_TEMPLATE: dict[str, Any] = {
    "schema_id": "action-template.v1",
    "action": "task_create",
    "input": {"title": "Scheduled digest", "domain": "research", "risk": "LOW"},
}
FIXED_AGENT_SELECTION: dict[str, Any] = {"mode": "fixed", "agent_id": ""}
CAPABILITY_SELECTION: dict[str, Any] = {
    "mode": "capability",
    "required_capabilities": ["cap-research"],
}


@dataclass
class Fixture:
    """One seeded Workspace plus helpers to create Schedules, versions and Runs."""

    seed: Seed
    engine: Engine
    clock: FixedClock

    @classmethod
    def create(cls, engine: Engine, tag: str, clock: FixedClock | None = None) -> Fixture:
        seed = Seed(tag)
        seed.create(engine)
        return cls(seed, engine, clock or FixedClock(T0))

    # ---------------------------------------------------------------- runtime / context

    def runtime(self) -> Runtime:
        return Runtime(
            make_session_factory(self.engine),
            AllowAllAuthorizer(),
            None,
            self.clock,
            str(self.seed.ws),
        )

    def ctx(
        self, session: Session, *, runner_id: str = "runner-1", authorizer: Any = None
    ) -> ExecutionContext:
        return ExecutionContext(
            session=session,
            store=DbRunStore(self.clock),
            event_store=PostgresEventStore(session, clock=self.clock),
            clock=self.clock,
            workspace_id=str(self.seed.ws),
            runner_id=runner_id,
            actor=self.seed.principal(self.seed.system),
            authorizer=AllowAllAuthorizer() if authorizer is None else authorizer,
        )

    # ---------------------------------------------------------------- schedules and runs

    def schedule(
        self,
        session: Session,
        schedule_id: str,
        *,
        status: str = "ENABLED",
        concurrency: str = "FORBID",
        missed_run: str = "RUN_ONCE",
        agent_selection: dict[str, Any] | None = None,
        action_template: dict[str, Any] | None = None,
        budget_policy: dict[str, Any] | None = None,
        retry_policy: dict[str, Any] | None = None,
        max_duration_seconds: int = 3600,
        execution_principal: str | None = None,
        ends_at: dt.datetime | None = None,
        backfill_limit: int = 0,
        backfill_window_seconds: int = 0,
    ) -> str:
        """Create a Schedule with version 1 pinned; returns the public schedule_version_id."""
        sid, vid = uuid.uuid4(), uuid.uuid4()
        version_public = f"sv-{schedule_id}-1"
        principal = self.seed.accounts[execution_principal or self.seed.owner]
        selection = agent_selection if agent_selection is not None else CAPABILITY_SELECTION
        if selection.get("mode") == "fixed" and not selection.get("agent_id"):
            selection = {**selection, "agent_id": self.seed.agent_id}
        template = action_template or ACTION_TEMPLATE
        content = {"schedule_id": schedule_id, "version": 1, "template": template}
        digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        session.execute(
            text(
                "INSERT INTO schedules (id, schedule_id, workspace_id, name, status, created_by) "
                "VALUES (:i, :s, :w, :n, :st, :by)"
            ),
            {
                "i": sid,
                "s": schedule_id,
                "w": self.seed.ws,
                "n": schedule_id,
                "st": status,
                "by": self.seed.accounts[self.seed.owner],
            },
        )
        session.execute(
            text(
                "INSERT INTO schedule_versions (id, schedule_version_id, schedule_id, version, "
                "name, channel_id, cron_expression, timezone, execution_principal_id, "
                "agent_selection, action_template, concurrency_policy, missed_run_policy, "
                "backfill_limit, backfill_window_seconds, max_duration_seconds, retry_policy, "
                "budget_policy, documentation_policy, starts_at, ends_at, snapshot_hash, "
                "created_by) VALUES (:i, :sv, :s, 1, :n, :c, '*/5 * * * *', 'UTC', :p, "
                "CAST(:sel AS jsonb), CAST(:tpl AS jsonb), :cc, :mr, :bl, :bw, :md, "
                "CAST(:rp AS jsonb), CAST(:bp AS jsonb), CAST('{}' AS jsonb), NULL, :ends, :h, :by)"
            ),
            {
                "i": vid,
                "sv": version_public,
                "s": schedule_id,
                "n": schedule_id,
                "c": self.seed.channel,
                "p": principal,
                "sel": json.dumps(selection),
                "tpl": json.dumps(template),
                "cc": concurrency,
                "mr": missed_run,
                "bl": backfill_limit,
                "bw": backfill_window_seconds,
                "md": max_duration_seconds,
                "rp": json.dumps(retry_policy or {"max_attempts": 3}),
                "bp": json.dumps(budget_policy or {}),
                "ends": ends_at,
                "h": digest,
                "by": self.seed.accounts[self.seed.owner],
            },
        )
        session.execute(
            text("UPDATE schedules SET current_version_id = :v WHERE schedule_id = :s"),
            {"v": vid, "s": schedule_id},
        )
        return version_public

    def run(
        self,
        session: Session,
        schedule_id: str,
        *,
        run_id: str | None = None,
        status: str = "CLAIMED",
        run_kind: str = "SCHEDULED",
        scheduled_for: dt.datetime | None = None,
        occurrence_key: str | None = None,
        idempotency_key: str | None = None,
        task_id: str | None = None,
        attempt_count: int = 0,
        started_at: dt.datetime | None = None,
        cancel_requested_at: dt.datetime | None = None,
        retry_of_run_id: str | None = None,
    ) -> RunLike:
        """Insert a Run in the given state and return it as the execution package sees it."""
        rid = run_id or f"run-{uuid.uuid4().hex[:16]}"
        when = scheduled_for or self.clock.now()
        version = session.execute(
            text(
                "SELECT id, snapshot_hash FROM schedule_versions WHERE schedule_id = :s "
                "ORDER BY version DESC LIMIT 1"
            ),
            {"s": schedule_id},
        ).first()
        assert version is not None
        key = occurrence_key
        if run_kind == "SCHEDULED" and key is None:
            key = hashlib.sha256(f"{schedule_id}|{rid}|{when.isoformat()}".encode()).hexdigest()
        session.execute(
            text(
                "INSERT INTO schedule_runs (id, run_id, workspace_id, schedule_id, "
                "schedule_version_id, run_kind, occurrence_key, scheduled_for, retry_of_run_id, "
                "status, attempt_count, task_id, idempotency_key, version_hash, claimed_by, "
                "claimed_at, lease_expires_at, started_at, cancel_requested_at, created_at, "
                "updated_at) VALUES (:i, :r, :w, :s, :v, :k, :ok, :when, :retry, :st, :ac, :task, "
                ":idem, :h, :by, :at, :lease, :started, :cancel, :now, :now)"
            ),
            {
                "i": uuid.uuid4(),
                "r": rid,
                "w": self.seed.ws,
                "s": schedule_id,
                "v": version[0],
                "k": run_kind,
                "ok": key,
                "when": when,
                "retry": retry_of_run_id,
                "st": status,
                "ac": attempt_count,
                "task": task_id,
                "idem": idempotency_key or f"idem-{rid}",
                "h": version[1],
                "by": "runner-1" if status == "CLAIMED" else None,
                "at": self.clock.now() if status == "CLAIMED" else None,
                "lease": self.clock.now() + dt.timedelta(seconds=60)
                if status == "CLAIMED"
                else None,
                "started": started_at,
                "cancel": cancel_requested_at,
                "now": self.clock.now(),
            },
        )
        return DbRunStore(self.clock).load_run(session, rid)

    # ---------------------------------------------------------------- assertions helpers

    def reload(self, session: Session, run_id: str) -> RunLike:
        return DbRunStore(self.clock).load_run(session, run_id)

    def run_events(self, session: Session, run_id: str) -> list[str]:
        return [
            str(r[0])
            for r in session.execute(
                text("SELECT type FROM events WHERE aggregate_id = :a ORDER BY aggregate_seq"),
                {"a": run_id},
            ).all()
        ]

    def tasks(self, session: Session) -> list[str]:
        return [
            str(r[0])
            for r in session.execute(
                text(
                    "SELECT task_id FROM tasks_projection WHERE workspace_id = :w ORDER BY task_id"
                ),
                {"w": self.seed.ws},
            ).all()
        ]

    def attempts(self, session: Session, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(r._mapping)
            for r in session.execute(
                text(
                    "SELECT attempt_no, result, error_code FROM schedule_run_attempts "
                    "WHERE run_id = :r ORDER BY attempt_no"
                ),
                {"r": run_id},
            ).all()
        ]

    def finish_task(self, session: Session, task_id: str, status: str = "COMPLETED") -> None:
        """Move a Task projection row to a terminal state (the Adapter/verifier path in short)."""
        session.execute(
            text("UPDATE tasks_projection SET status = :st, updated_at = :now WHERE task_id = :t"),
            {"st": status, "now": self.clock.now(), "t": task_id},
        )

    def report_usage(
        self, session: Session, run_id: str, cost_units: int, task_id: str | None = None
    ) -> None:
        """Record usage for a Run (§7C settlement input)."""
        session.execute(
            text(
                "INSERT INTO usage_records (workspace_id, account_id, agent_id, task_id, run_id, "
                "model, input_tokens, output_tokens, tool_calls, wall_ms, cost_units, source, "
                "pricing_version, reported_at) VALUES (:w, :a, :g, :t, :r, 'm', 0, 0, 0, 0, :c, "
                "'reported', :pv, :now)"
            ),
            {
                "w": self.seed.ws,
                "a": self.seed.accounts[f"acct-{self.seed.tag}-agent"],
                "g": self.seed.agent_id,
                "t": task_id,
                "r": run_id,
                "c": cost_units,
                "pv": pricing_version(session),
                "now": self.clock.now(),
            },
        )


def pricing_version(session: Session) -> str:
    """The active pricing version (the usage tables reference it)."""
    row = session.execute(
        text("SELECT pricing_version FROM pricing_versions ORDER BY activated_at DESC LIMIT 1")
    ).first()
    if row is not None:
        return str(row[0])
    from server.usage.versions import activate_from_file

    activate_from_file(session)
    row = session.execute(
        text("SELECT pricing_version FROM pricing_versions ORDER BY activated_at DESC LIMIT 1")
    ).first()
    assert row is not None
    return str(row[0])


def execute_run(fx: Fixture, run: RunLike, *, session: Session) -> Any:
    return execution.execute(run, fx.ctx(session))


def advance(clock: Clock, seconds: float) -> None:
    assert isinstance(clock, FixedClock)
    clock.advance(dt.timedelta(seconds=seconds))
