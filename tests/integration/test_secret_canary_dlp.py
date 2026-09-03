"""V-P4-14: a full flow with a canary secret (grant → lease → resolve by the adapter → progress
and result → card render → document draft) leaves zero canaries in Events, audit metadata,
outbox rows, messages, work items, documents, task projections, notifications, logs and errors."""

from __future__ import annotations

import datetime as dt
import logging
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.errors import ApiError
from server.application import secrets as sc
from server.application import tasks as t
from server.application import work as w
from server.application.criteria import current_criteria
from server.channels.outbox import RecordingChannelProvider, drain_channels
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import FixedClock
from server.secrets import canary
from server.secrets.injection import InMemoryHandleStore, install_log_filter
from server.secrets.provider import ResolveContext
from tests.integration.secrets_seed import MASTER, T0, Seed

pytestmark = pytest.mark.db
SEED = Seed("can")
CRITERIA = ({"statement": "done", "check_type": "evidence", "required": True},)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    SEED.create(eng)
    yield eng
    eng.dispose()


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def test_full_flow_leaves_zero_canaries(engine: Engine) -> None:
    clock = FixedClock(T0)
    rt = SEED.runtime(engine, clock)
    agent = SEED.register_agent(engine, rt, "agent-can-1")
    collector = _Collect()
    root = logging.getLogger()
    root.addHandler(collector)
    install_log_filter(canary.registered_values)  # scrubs every root handler, this one included
    try:
        marker = canary.canary_value(42)
        ref = SEED.run(
            rt, SEED.admin_p, sc.RegisterSecret("canary/42", marker.encode()), "reg"
        ).resource_id
        canary.register_canary(ref, 42)
        task = SEED.run(
            rt,
            SEED.admin_p,
            t.CreateTask("canary task", str(SEED.channel), "ops", "LOW", criteria=CRITERIA),
            "task",
        ).resource_id
        SEED.run(rt, SEED.admin_p, sc.CreateSecretGrant(ref, "agent-can-1", task_id=task), "grant")
        SEED.run(rt, SEED.admin_p, t.DelegateTask(task, agent.account_id), "delegate")
        SEED.run(rt, agent, t.AcceptTask(task), "accept")
        SEED.run(rt, agent, t.StartTask(task), "start")
        lease = SEED.run(rt, agent, sc.IssueSecretLease(ref, task_id=task), "lease").data
        store = InMemoryHandleStore(
            make_session_factory(engine), MASTER, workspace_id=SEED.ws, clock=clock
        )
        try:
            got = store.resolve(lease["handle"], ResolveContext("agent-can-1", task_id=task))
            assert got.decode() == marker
            logging.getLogger("adapter").warning(
                "resolved handle for task %s: %s", task, got.decode()
            )
            # the adapter reports progress and submits; a careless adapter echoing the value is
            # exactly what the DLP must catch — the server-side surfaces must stay clean
            SEED.run(
                rt,
                agent,
                t.ReportProgress(task, f"deployed with lease {lease['lease_id']}"),
                "progress",
            )
            errors: list[str] = []
            try:  # a wrong second resolve: the error text must not carry the value either
                store.resolve(lease["handle"], ResolveContext("agent-can-1", task_id=task))
            except Exception as exc:
                errors.append(str(exc))
            with Session(engine) as s:
                current = current_criteria(s, task)
            refs = tuple(f"{c.criteria_id}:evidence/canary" for c in current.criteria)
            SEED.run(
                rt,
                agent,
                t.SubmitImplementation(task, refs, criteria_revision=current.revision),
                "submit",
            )
        finally:
            store.close()
        with Session(engine) as s, s.begin():
            drain_channels(
                s,
                {"mattermost": RecordingChannelProvider(prefix="mattermost")},
                clock,
                str(SEED.ws),
            )
        with Session(engine) as s:
            hits = canary.scan(
                s,
                SEED.ws,
                log_lines=collector.lines,
                error_texts=errors,
                document_root=Path(os.environ.get("AGENT_COLAB_DOCUMENT_ROOT", "/nonexistent")),
            )
        assert hits == [], canary.summarize(hits)
        assert any("<secret-redacted>" in line for line in collector.lines)  # the log was scrubbed
        with Session(
            engine
        ) as s:  # the Events/audit for this flow exist (the scan was not vacuous)
            assert (
                s.execute(
                    text(
                        "SELECT count(*) FROM events WHERE workspace_id = :w AND "
                        "type = 'SECRET_ACCESSED'"
                    ),
                    {"w": SEED.ws},
                ).scalar_one()
                == 1
            )
    finally:
        root.removeHandler(collector)
        canary.clear_registry()


def test_scan_detects_a_planted_canary(engine: Engine) -> None:
    """The scanner is not vacuous: a canary planted in an audit row is reported by location."""
    marker = canary.canary_value(7)
    canary.register_canary("sec-planted", 7)
    try:
        from server.observability.audit import append_audit

        with Session(engine) as s, s.begin():
            append_audit(
                s,
                action="test.plant",
                target_type="test",
                target_id="x",
                result="OK",
                actor_label="t",
                correlation_id="c",
                workspace_id=SEED.ws,
                metadata={"note": marker},
            )
            hits = canary.scan(s, SEED.ws, log_lines=[f"log line with {marker}"])
        assert {h.location.split(":")[0] for h in hits} >= {"audit_events.redacted_metadata", "log"}
        assert all(h.value_ref == "sec-planted" for h in hits)
        assert marker not in str([h.location for h in hits])
    finally:
        canary.clear_registry()


def _unused(_: dt.datetime, __: uuid.UUID, ___: ApiError, ____: w.WorkPoll) -> None:
    return None
