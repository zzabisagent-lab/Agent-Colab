"""Shared seeding for the Phase 6 documentation tests (workspace, Task flow, Schedule Runs)."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application import bus
from server.application.authz import AllowAllAuthorizer
from server.application.criteria import current_criteria
from server.application.tasks import (
    AcceptTask,
    CreateTask,
    DelegateTask,
    StartTask,
    StartVerification,
    SubmitImplementation,
)
from server.application.verification import (
    AssignVerifier,
    CreateVerificationRun,
    RequestRecheck,
    SubmitFix,
    SubmitVerdict,
)
from server.application.verification import StartVerification as StartRun
from server.documents.store import DocumentStore
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore

T0 = dt.datetime(2026, 5, 4, 9, 0, tzinfo=dt.UTC)  # a Monday
CRITERIA = ({"statement": "report attached", "check_type": "evidence", "required": True},)


def report(result: str, risks: list[str] | None = None) -> dict[str, Any]:
    return {
        "result": result,
        "criteria_version": "v8.0",
        "tests": [
            {
                "id": "V-P6-12",
                "result": "PASS" if result == "PASSED" else "FAIL",
                "evidence_ref": "e/1",
            }
        ],
        "findings": []
        if result == "PASSED"
        else [{"id": "F-1", "severity": "High", "summary": "evidence incomplete"}],
        "residual_risks": risks or [],
    }


class DocSeed:
    """A workspace with an admin, an implementer, a verifier and a documentation Agent."""

    def __init__(self, tag: str, root: Path) -> None:
        self.tag = tag
        self.ws = uuid.uuid4()
        self.channel = uuid.uuid4()
        self.channel_id = f"chan-{tag}"
        self.clock = FixedClock(T0)
        self.store = DocumentStore(root / tag)
        self.accounts: dict[str, uuid.UUID] = {}
        self.doc_agent = f"agent-{tag}-writer"

    # ---- names -------------------------------------------------------------------------
    @property
    def admin(self) -> str:
        return f"acct-{self.tag}-admin"

    @property
    def impl(self) -> str:
        return f"acct-{self.tag}-impl"

    @property
    def ver(self) -> str:
        return f"acct-{self.tag}-ver"

    @property
    def writer(self) -> str:
        return f"acct-{self.tag}-writer"

    # ---- seeding -----------------------------------------------------------------------
    def create(self, engine: Engine, *, with_doc_agent: bool = False) -> None:
        with engine.begin() as c:
            c.execute(
                text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, :w, :w)"),
                {"i": self.ws, "w": f"ws-{self.tag}"},
            )
            for name, typ in (
                (self.admin, "service"),
                (self.impl, "agent"),
                (self.ver, "agent"),
                (self.writer, "agent"),
            ):
                self._account(c, name, typ)
            c.execute(
                text(
                    "INSERT INTO channels (id, channel_id, workspace_id, channel_type, "
                    "display_name) VALUES (:i, :c, :w, 'work', :c)"
                ),
                {"i": self.channel, "c": self.channel_id, "w": self.ws},
            )
            for name in (self.admin, self.impl, self.ver, self.writer):
                c.execute(
                    text(
                        "INSERT INTO channel_members (channel_id, account_id, permissions) "
                        "VALUES (:c, :a, CAST(:p AS jsonb))"
                    ),
                    {
                        "c": self.channel,
                        "a": self.accounts[name],
                        "p": json.dumps(["read", "write"]),
                    },
                )
            if with_doc_agent:
                self._doc_agent(c)

    def _account(self, c: Any, name: str, typ: str) -> uuid.UUID:
        acc = uuid.uuid4()
        self.accounts[name] = acc
        c.execute(
            text(
                "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
                "VALUES (:i, :a, :w, :t, :a)"
            ),
            {"i": acc, "a": name, "w": self.ws, "t": typ},
        )
        return acc

    def _doc_agent(self, c: Any) -> None:
        c.execute(
            text(
                "INSERT INTO agents (id, agent_id, workspace_id, account_id, adapter_type, "
                "status, display_name, online, capacity) VALUES (:i, :g, :w, :a, 'mcp', 'active', "
                ":g, true, 2)"
            ),
            {"i": uuid.uuid4(), "g": self.doc_agent, "w": self.ws, "a": self.accounts[self.writer]},
        )
        c.execute(
            text(
                "INSERT INTO capabilities (id, capability_id, tool, domain) "
                "VALUES (:i, :c, 'document.narrate', 'documentation') "
                "ON CONFLICT (capability_id) DO NOTHING"
            ),
            {"i": uuid.uuid4(), "c": f"cap-narrate-{self.tag}"},
        )
        c.execute(
            text("INSERT INTO agent_capabilities (agent_id, capability_id) VALUES (:g, :c)"),
            {"g": self.doc_agent, "c": f"cap-narrate-{self.tag}"},
        )

    # ---- command plumbing --------------------------------------------------------------
    def ctx(self, session: Session, who: str, key: str) -> bus.CommandContext:
        typ = "service" if who == self.admin else "agent"
        return bus.CommandContext(
            session=session,
            store=PostgresEventStore(session, clock=self.clock),
            authorizer=AllowAllAuthorizer(),
            clock=self.clock,
            principal=bus.Principal(who, str(self.accounts[who]), typ, f"sha256:{who}"),
            workspace_id=str(self.ws),
            correlation_id=f"corr-{self.tag}",
            idempotency_key=key,
            extras={"document_store": self.store},
        )

    # ---- Task flow ---------------------------------------------------------------------
    def implement(self, s: Session, key: str, title: str, *, submit: bool = True) -> str:
        created = bus.execute(
            CreateTask(title, str(self.channel), "research", criteria=CRITERIA),
            self.ctx(s, self.admin, f"{key}-create"),
        )
        task_id: str = created.resource_id
        bus.execute(DelegateTask(task_id, self.impl), self.ctx(s, self.admin, f"{key}-delegate"))
        bus.execute(AcceptTask(task_id), self.ctx(s, self.impl, f"{key}-accept"))
        bus.execute(StartTask(task_id), self.ctx(s, self.impl, f"{key}-start"))
        if submit:
            self.submit(s, task_id, f"{key}-submit")
        return task_id

    def submit(self, s: Session, task_id: str, key: str) -> None:
        current = current_criteria(s, task_id)
        refs = tuple(f"{c.criteria_id}:evidence/{key}" for c in current.criteria)
        bus.execute(
            SubmitImplementation(task_id, refs, criteria_revision=current.revision),
            self.ctx(s, self.impl, key),
        )

    def verification(self, s: Session, task_id: str, key: str) -> str:
        run = bus.execute(
            CreateVerificationRun(
                target_type="task",
                target_id=task_id,
                implementer_account_id=self.impl,
                verifier_account_id=self.ver,
                implementer_credential_fingerprint=f"sha256:{self.impl}",
                verifier_credential_fingerprint=f"sha256:{self.ver}",
                target_commit="abc123",
                effective_policy_hash="p",
                task_id=task_id,
            ),
            self.ctx(s, self.admin, f"{key}-vr"),
        )
        vid: str = run.resource_id
        bus.execute(StartVerification(task_id, vid), self.ctx(s, self.admin, f"{key}-verifying"))
        bus.execute(AssignVerifier(vid), self.ctx(s, self.admin, f"{key}-assign"))
        bus.execute(StartRun(vid), self.ctx(s, self.ver, f"{key}-run"))
        return vid

    def verdict(
        self, s: Session, vid: str, result: str, key: str, risks: list[str] | None = None
    ) -> None:
        bus.execute(SubmitVerdict(vid, result, report(result, risks)), self.ctx(s, self.ver, key))

    def recheck(self, s: Session, task_id: str, vid: str, key: str) -> None:
        status = s.execute(
            text("SELECT status FROM tasks_projection WHERE task_id = :t"), {"t": task_id}
        ).scalar_one()
        if status == "WAITING":
            bus.execute(StartTask(task_id), self.ctx(s, self.impl, f"{key}-resume"))
        self.submit(s, task_id, f"{key}-resubmit")
        bus.execute(StartVerification(task_id, vid), self.ctx(s, self.admin, f"{key}-verifying"))
        bus.execute(SubmitFix(vid, "def456"), self.ctx(s, self.impl, f"{key}-fix"))
        bus.execute(RequestRecheck(vid), self.ctx(s, self.admin, f"{key}-recheck"))
        bus.execute(StartRun(vid), self.ctx(s, self.ver, f"{key}-run"))

    # ---- Schedule Run ------------------------------------------------------------------
    def schedule_run(
        self,
        s: Session,
        tag: str,
        *,
        status: str = "SUCCEEDED",
        task_id: str | None = None,
        error_code: str | None = None,
    ) -> str:
        """A minimal Schedule + version + terminal Run (the Phase 5 tables, seeded directly)."""
        schedule_id = f"sch-{self.tag}-{tag}"
        version_uuid = uuid.uuid4()
        s.execute(
            text(
                "INSERT INTO schedules (id, schedule_id, workspace_id, name, status, created_by) "
                "VALUES (:i, :s, :w, :n, 'ENABLED', :b) ON CONFLICT (schedule_id) DO NOTHING"
            ),
            {
                "i": uuid.uuid4(),
                "s": schedule_id,
                "w": self.ws,
                "n": f"Nightly {tag}",
                "b": self.accounts[self.admin],
            },
        )
        s.execute(
            text(
                "INSERT INTO schedule_versions (id, schedule_version_id, schedule_id, version, "
                "name, channel_id, cron_expression, timezone, execution_principal_id, "
                "agent_selection, action_template, concurrency_policy, missed_run_policy, "
                "backfill_limit, backfill_window_seconds, max_duration_seconds, "
                "min_interval_minutes, retry_policy, budget_policy, documentation_policy, "
                "snapshot_hash, created_by, event_id) "
                "VALUES (:i, :sv, :s, 1, :n, :c, '0 2 * * *', 'UTC', :p, CAST(:a AS jsonb), "
                "CAST(:t AS jsonb), 'FORBID', 'RUN_ONCE', 0, 0, 3600, 5, CAST(:r AS jsonb), "
                "CAST(:b AS jsonb), CAST(:d AS jsonb), :h, :cb, NULL)"
            ),
            {
                "i": version_uuid,
                "sv": f"schv-{uuid.uuid4().hex[:12]}",
                "s": schedule_id,
                "n": f"Nightly {tag}",
                "c": self.channel,
                "p": self.accounts[self.admin],
                "a": json.dumps({"mode": "capability", "required_capabilities": ["cap-research"]}),
                "t": json.dumps(
                    {
                        "schema_id": "action-template.v1",
                        "action": "task_create",
                        "input": {"title": "Nightly", "domain": "research"},
                    }
                ),
                "r": json.dumps({"max_attempts": 3, "backoff_seconds": [1, 5, 25]}),
                "b": json.dumps({"per_run_cost_units": 100, "daily_cost_units": 1000}),
                "d": json.dumps({"draft": True, "period_summary": "daily"}),
                "h": "0" * 64,
                "cb": self.accounts[self.admin],
            },
        )
        s.execute(
            text("UPDATE schedules SET current_version_id = :v WHERE schedule_id = :s"),
            {"v": version_uuid, "s": schedule_id},
        )
        run_id = f"run-{self.tag}-{tag}"
        s.execute(
            text(
                "INSERT INTO schedule_runs (id, run_id, workspace_id, schedule_id, "
                "schedule_version_id, run_kind, occurrence_key, scheduled_for, status, "
                "attempt_count, task_id, idempotency_key, version_hash, started_at, finished_at, "
                "error_code, requested_by) VALUES (:i, :r, :w, :s, :v, 'SCHEDULED', :o, :at, "
                ":st, 1, :t, :k, :h, :at, :at, :e, :b)"
            ),
            {
                "i": uuid.uuid4(),
                "r": run_id,
                "w": self.ws,
                "s": schedule_id,
                "v": version_uuid,
                "o": f"occ-{uuid.uuid4().hex[:12]}",
                "at": T0,
                "st": status,
                "t": task_id,
                "k": f"idem-{uuid.uuid4().hex[:12]}",
                "h": "0" * 64,
                "e": error_code,
                "b": self.accounts[self.admin],
            },
        )
        s.execute(
            text(
                "INSERT INTO schedule_run_attempts (id, run_id, attempt_no, result, error_code, "
                "started_at, finished_at) VALUES (:i, :r, 1, :s, :e, :at, :at)"
            ),
            {"i": uuid.uuid4(), "r": run_id, "s": status, "e": error_code, "at": T0},
        )
        return run_id


__all__ = ["CRITERIA", "T0", "DocSeed", "report"]
