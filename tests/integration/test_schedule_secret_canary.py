"""V-P5-17 (finding F-P5-002): a scheduled Run that uses a Secret reference gets a short,
single-use lease that is revoked when the Run ends, and the secret *value* never leaks.

Unlike the lease-metadata test, this one puts a real (synthetic) value through the whole path:
the local provider encrypts it, the Broker grants and leases it, the injection store resolves it
for the Run's Agent, and the Run finishes. Afterwards a canary scan over every persisted and
emitted surface — Events, audit metadata, Tasks, Schedule Runs/attempts/notices/versions, outbox
rows, channel posts, work items, usage records, budget alerts, documents on disk and the captured
application log — must find the marker nowhere. The marker is ``CANARY-NOT-A-SECRET-<n>``, which
is a documented non-secret in the repository's secret-scan allowlist.
"""

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

from server.db.engine import make_engine, make_session_factory
from server.events.postgres_store import PostgresEventStore
from server.schedules import execution
from server.schedules.contract import RunStatus
from server.secrets import broker, canary
from server.secrets import local_provider as lp
from server.secrets.envelope import MasterKey, new_master_key
from server.secrets.injection import InMemoryHandleStore, install_log_filter
from server.secrets.provider import LeaseScope, ResolveContext, SecretError
from tests.integration.schedule_exec_fixture import CAPABILITY_SELECTION, Fixture

pytestmark = pytest.mark.db
MASTER = MasterKey.from_b64("mk-canary-1", new_master_key())
CANARY_N = 5170  # V-P5-17
ACTION = "task_create"


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture
def fx(engine: Engine) -> Iterator[Fixture]:
    fixture = Fixture.create(engine, f"canary{uuid.uuid4().hex[:6]}")
    yield fixture
    canary.clear_registry()


def _enable_server_loggers() -> None:
    """Re-enable the application loggers so the log scan is not vacuous.

    A ``dictConfig`` with ``disable_existing_loggers`` (uvicorn's default, used by the end-to-end
    tests that start a server in this process) leaves every existing logger disabled, which would
    silently make any log-leak assertion pass for the wrong reason.
    """
    logging.getLogger().disabled = False
    for name, logger in logging.root.manager.loggerDict.items():
        if isinstance(logger, logging.Logger) and name.startswith(("server", "sidecar")):
            logger.disabled = False


def _template(secret_ref: str) -> dict[str, object]:
    return {
        "schema_id": "action-template.v1",
        "action": ACTION,
        "input": {"title": "run that uses a secret", "domain": "research"},
        "secret_refs": [secret_ref],
    }


def _register_canary_secret(fx: Fixture, session: Session) -> tuple[str, str]:
    """Store the synthetic canary through the local provider; returns (secret_ref, value)."""
    ref = f"sec-canary-{uuid.uuid4().hex[:12]}"
    value = canary.register_canary(ref, CANARY_N)
    lp.put_secret(
        session,
        MASTER,
        workspace_id=fx.seed.ws,
        name=f"canary-{uuid.uuid4().hex[:8]}",
        value=value.encode(),
        metadata={"purpose": "schedule canary", "owner": "ops"},
        created_by=fx.seed.accounts[fx.seed.owner],
        now=fx.clock.now(),
        secret_ref=ref,
    )
    broker.create_grant(
        session,
        workspace_id=fx.seed.ws,
        secret_ref=ref,
        agent_id=fx.seed.agent_id,
        task_id=None,
        action=None,
        ttl_seconds=300,
        single_use=True,
        valid_for=dt.timedelta(hours=1),
        created_by=fx.seed.accounts[fx.seed.owner],
        now=fx.clock.now(),
        store=PostgresEventStore(session, clock=fx.clock),
        correlation_id="canary-grant",
        idempotency_key=f"grant-{uuid.uuid4().hex[:10]}",
    )
    return ref, value


def test_run_secret_value_is_leased_revoked_and_never_leaks(
    fx: Fixture, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _enable_server_loggers()  # another module's dictConfig may have disabled them
    # the runtime scrubber, installed by server.main at startup, protects the log surface
    install_log_filter(canary.registered_values)
    with Session(engine) as s, s.begin():
        secret_ref, value = _register_canary_secret(fx, s)
        fx.schedule(
            s,
            "sch-canary",
            agent_selection=dict(CAPABILITY_SELECTION),
            action_template=_template(secret_ref),
        )
        run = fx.run(s, "sch-canary", run_id="run-canary-1")
        outcome = execution.execute(run, fx.ctx(s))
        assert outcome.status == RunStatus.TASK_CREATED.value, outcome.detail
        task_id = str(outcome.task_id)
        # the Run's own lease: short-lived and single use (§9.3)
        run_lease = s.execute(
            text(
                "SELECT lease_id, expires_at, single_use, revoked_at FROM secret_leases "
                "WHERE task_id = :t AND secret_ref = :r"
            ),
            {"t": task_id, "r": secret_ref},
        ).all()
        assert len(run_lease) == 1 and run_lease[0][2] is True and run_lease[0][3] is None
        assert (run_lease[0][1] - fx.clock.now()).total_seconds() <= 300

    # the Agent side: a handle for the same scope resolves the value exactly once
    factory = make_session_factory(engine)
    store = InMemoryHandleStore(factory, MASTER, workspace_id=fx.seed.ws, clock=fx.clock)
    try:
        with Session(engine) as s, s.begin():
            lease = broker.issue_lease(
                s,
                workspace_id=fx.seed.ws,
                secret_ref=secret_ref,
                scope=LeaseScope(agent_id=fx.seed.agent_id, task_id=task_id, action=ACTION),
                ttl=dt.timedelta(seconds=300),
                single_use=True,
                now=fx.clock.now(),
                actor_label=f"agent:{fx.seed.agent_id}",
                correlation_id="run-canary-1",
            )
            outstanding = broker.issue_lease(
                s,
                workspace_id=fx.seed.ws,
                secret_ref=secret_ref,
                scope=LeaseScope(agent_id=fx.seed.agent_id, task_id=task_id, action=ACTION),
                ttl=dt.timedelta(seconds=300),
                single_use=True,
                now=fx.clock.now(),
                actor_label=f"agent:{fx.seed.agent_id}",
                correlation_id="run-canary-1-outstanding",
            )
        context = ResolveContext(agent_id=fx.seed.agent_id, task_id=task_id, action=ACTION)
        assert store.resolve(lease.handle, context) == value.encode()  # the value really flows
        assert store.holds(lease.lease_id)
        with pytest.raises(SecretError) as second:
            store.resolve(lease.handle, context)
        assert second.value.code == "SECRET_HANDLE_USED"

        # the Run ends: every lease of the Task is revoked and the buffer is wiped
        with Session(engine) as s, s.begin():
            fx.finish_task(s, task_id, "COMPLETED")
            execution.on_task_terminal(fx.ctx(s), task_id, "COMPLETED")
        with Session(engine) as s:
            open_leases = s.execute(
                text(
                    "SELECT count(*) FROM secret_leases WHERE task_id = :t AND revoked_at IS NULL"
                ),
                {"t": task_id},
            ).scalar_one()
        assert open_leases == 0
        assert not store.holds(lease.lease_id)
        assert all(value.encode() not in buf for buf in store.live_values())

        # a handle that was still outstanding when the Run ended no longer resolves
        with pytest.raises(SecretError) as revoked:
            store.resolve(outstanding.handle, context)
        assert revoked.value.code == "SECRET_HANDLE_REVOKED"

    finally:
        store.close()

    # zero leakage across every persisted and emitted surface
    with Session(engine) as s:
        hits = canary.scan(
            s,
            fx.seed.ws,
            log_lines=caplog.messages,
            document_root=Path(os.environ["AGENT_COLAB_DOCUMENT_ROOT"]),
        )
        artifact_hits = canary.scan(
            s, fx.seed.ws, document_root=Path(os.environ["AGENT_COLAB_ARTIFACT_ROOT"])
        )
        stored = s.execute(
            text(
                "SELECT count(*) FROM secret_versions WHERE secret_ref = :r "
                "AND position(CAST(:v AS bytea) in ciphertext) > 0"
            ),
            {"r": secret_ref, "v": value.encode()},
        ).scalar_one()
    # positive control: the scanner really does detect the marker, so zero hits means something
    assert len(canary.scan_text([f"a component logged {value}"])) == 1
    assert canary.summarize(hits) == {"hits": 0, "locations": []}
    assert canary.summarize(artifact_hits) == {"hits": 0, "locations": []}
    assert stored == 0  # at rest the value exists only as ciphertext

    # the log surface is not merely quiet: a component that did log the value is scrubbed
    install_log_filter(canary.registered_values)  # caplog's handler joined after the flow started
    _enable_server_loggers()
    logging.getLogger("server.schedules.execution").warning("run leaked %s", value)
    assert caplog.messages, "no log output was captured, so the log scan would be vacuous"
    assert canary.summarize(canary.scan_text(caplog.messages)) == {"hits": 0, "locations": []}
