"""V-P4-13 / P4-07: in-memory injection resolves through the Broker, wipes bytes the moment a
lease is revoked (well within 5 s) and the log scrubber never lets a live value reach a record;
the Mattermost bot adapter refuses secret-carrying items."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from server.agents.adapters.contract import WorkItemView
from server.agents.adapters.mattermost_bot import MattermostBotAdapter
from server.application import secrets as sc
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import FixedClock
from server.secrets.injection import InMemoryHandleStore, SecretLogFilter
from server.secrets.provider import ResolveContext, SecretError
from tests.integration.secrets_seed import MASTER, T0, Seed

pytestmark = pytest.mark.db
SEED = Seed("inj")


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    SEED.create(eng)
    yield eng
    eng.dispose()


def test_in_memory_store_resolves_once_and_wipes_on_revoke(engine: Engine) -> None:
    clock = FixedClock(T0)
    rt = SEED.runtime(engine, clock)
    agent = SEED.register_agent(engine, rt, "agent-inj-1")
    value = b"inj-value-" + uuid.uuid4().hex.encode()
    ref = SEED.run(rt, SEED.admin_p, sc.RegisterSecret("inj/key", value), "reg").resource_id
    gid = SEED.run(
        rt, SEED.admin_p, sc.CreateSecretGrant(ref, "agent-inj-1", single_use=False), "grant"
    ).data["grant_id"]
    lease = SEED.run(rt, agent, sc.IssueSecretLease(ref, work_item_id="wi-inj-1"), "lease").data
    store = InMemoryHandleStore(
        make_session_factory(engine), MASTER, workspace_id=SEED.ws, clock=clock
    )
    try:
        got = store.resolve(lease["handle"], ResolveContext("agent-inj-1", work_item_id="wi-inj-1"))
        assert got == value and store.holds(lease["lease_id"])
        # the scrubber hides the live value from any log record
        logger = logging.getLogger("test.secret.scrub")
        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append  # type: ignore[method-assign]
        handler.addFilter(SecretLogFilter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.info("adapter used %s for wi-inj-1", value.decode())
        logger.removeHandler(handler)
        assert records and value.decode() not in records[0].getMessage()
        assert "<secret-redacted>" in records[0].getMessage()
        # revocation (admin) → the buffer is zeroed immediately, before any 5 s poll
        started = dt.datetime.now(dt.UTC)
        SEED.run(rt, SEED.admin_p, sc.RevokeSecretGrant(gid, "grant", "ADMIN_REVOKE"), "rev")
        elapsed = (dt.datetime.now(dt.UTC) - started).total_seconds()
        assert not store.holds(lease["lease_id"]) and elapsed < 5
        assert store.wiped and store.wiped[-1][0] == lease["lease_id"]
        assert value not in store.live_values()
        with pytest.raises(SecretError) as exc:  # new resolves are rejected immediately
            store.resolve(lease["handle"], ResolveContext("agent-inj-1", work_item_id="wi-inj-1"))
        assert exc.value.code == "SECRET_HANDLE_REVOKED"
        assert value.decode() not in str(exc.value)
    finally:
        store.close()


def test_sidecar_bound_handle_cannot_be_resolved_from_another_host(engine: Engine) -> None:
    clock = FixedClock(T0 + dt.timedelta(minutes=5))
    rt = SEED.runtime(engine, clock)
    agent = SEED.ensure_agent(engine, rt, "agent-inj-1")
    ref = SEED.run(
        rt, SEED.admin_p, sc.RegisterSecret("inj/host", b"h-" + uuid.uuid4().hex.encode()), "reg-h"
    ).resource_id
    SEED.run(rt, SEED.admin_p, sc.CreateSecretGrant(ref, "agent-inj-1"), "grant-h")
    lease = SEED.run(
        rt, agent, sc.IssueSecretLease(ref, sidecar_instance_id="sc-host-a"), "lease-h"
    ).data
    store = InMemoryHandleStore(
        make_session_factory(engine), MASTER, workspace_id=SEED.ws, clock=clock
    )
    try:
        with pytest.raises(SecretError) as exc:
            store.resolve(
                lease["handle"], ResolveContext("agent-inj-1", sidecar_instance_id="sc-host-b")
            )
        assert exc.value.code == "SECRET_HANDLE_HOST_MISMATCH"
        assert store.resolve(
            lease["handle"], ResolveContext("agent-inj-1", sidecar_instance_id="sc-host-a")
        )
    finally:
        store.close()


def test_bot_adapter_refuses_secret_items() -> None:
    adapter = MattermostBotAdapter(
        {"agent_id": "agent-bot", "provider_instance_id": "mm:x", "bot_user_id": "b"}
    )
    item = WorkItemView(
        "wi-" + "0" * 24,
        "invoke",
        "agent-bot",
        None,
        "corr",
        T0 + dt.timedelta(hours=1),
        "colab://work/wi-000000000000000000000000/payload",
        ("sh-" + "a" * 32,),
        "colab.work-result.v1",
        "idem",
        {},
    )
    receipt = adapter.deliver(item)
    assert receipt.rejection_code == "CAPABILITY_UNSUPPORTED"
    assert adapter.probe().secret_handles == "unsupported"  # nosec B105 - advertisement value


def _unused(_: Session) -> None:  # keeps the Session import for type checkers
    return None
