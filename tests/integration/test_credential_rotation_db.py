"""V-P7-12: rotating Agent, Mattermost, Telegram and administrator credentials in sequence.

Each rotation confirms the new credential first, and from that moment the old one is rejected;
the timing is measured against the 60 s budget. Work continues across the whole sequence: the
Tasks, messages and work items created before, during and after every rotation are all present
exactly once at the end.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime, execute_command
from server.application import accounts as acc
from server.application import tasks as t
from server.application.authz import BusAuthorizer
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import FixedClock
from server.identity.principals import resolve_service_token, token_hash
from tests.integration.phase4_admin_seed import T0, Seed, seed

pytestmark = pytest.mark.db
ROTATION_BUDGET_S = 60  # V-P7-12: the old credential must be rejected inside this window
CRITERIA = ({"statement": "done", "check_type": "evidence", "required": True},)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def sd(engine: Engine) -> Seed:
    return seed(engine, "rot")


@pytest.fixture(scope="module")
def channel(engine: Engine, sd: Seed) -> str:
    channel_id = f"chan-rot-{uuid.uuid4().hex[:6]}"
    cid = uuid.uuid4()
    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name) "
                "VALUES (:i, :c, :w, 'work', 'rotation')"
            ),
            {"i": cid, "c": channel_id, "w": sd.ws},
        )
        for account in sd.accounts.values():
            s.execute(
                text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                {"c": cid, "a": account},
            )
    return str(cid)


def _runtime(engine: Engine, sd: Seed, clock: FixedClock) -> Runtime:
    return Runtime(make_session_factory(engine), BusAuthorizer(), None, clock, str(sd.ws))


def _accepts(engine: Engine, token: str) -> bool:
    """Whether the credential still authenticates, which is what a caller experiences."""
    with Session(engine) as s:
        return resolve_service_token(s, token) is not None


def _create_task(engine: Engine, sd: Seed, clock: FixedClock, channel: str, key: str) -> str:
    result = execute_command(
        _runtime(engine, sd, clock),
        sd.principal("admin1"),
        t.CreateTask(f"rotation {key}", channel, "research", "LOW", criteria=CRITERIA),
        idempotency_key=key,
        correlation_id=f"corr-{key}",
    )
    return str(result.resource_id)


def _provider_token_row(engine: Engine, sd: Seed, provider: str, token: str) -> str:
    """Provider bot credentials live as a Secret Broker reference; the row models the rotation."""
    ref = f"sec-{provider}-{uuid.uuid4().hex[:8]}"
    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, provider,"
                " base_url, team_or_bot_ref, config) VALUES (:i, :p, :w, :prov, 'http://x', :ref,"
                " CAST(:cfg AS jsonb))"
            ),
            {
                "i": uuid.uuid4(),
                "p": ref,
                "w": sd.ws,
                "prov": provider,
                "ref": ref,
                "cfg": f'{{"credential_ref": "{ref}", "token_fingerprint": "{token_hash(token)}"}}',
            },
        )
    return ref


def _provider_accepts(engine: Engine, ref: str, token: str) -> bool:
    with Session(engine) as s:
        stored = s.execute(
            text(
                "SELECT config->>'token_fingerprint' FROM provider_instances WHERE "
                "provider_instance_id = :p"
            ),
            {"p": ref},
        ).scalar_one()
    return bool(stored == token_hash(token))


def _rotate_provider(engine: Engine, ref: str, new_token: str) -> None:
    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "UPDATE provider_instances SET config = jsonb_set(config, "
                "'{token_fingerprint}', to_jsonb(CAST(:fp AS text))) "
                "WHERE provider_instance_id = :p"
            ),
            {"fp": token_hash(new_token), "p": ref},
        )


def test_sequential_rotation_rejects_old_credentials_and_loses_no_work(
    engine: Engine, sd: Seed, channel: str
) -> None:
    clock = FixedClock(T0 + dt.timedelta(days=1))
    created: list[str] = [_create_task(engine, sd, clock, channel, "rot-before")]

    # --- 1. an Agent service token -----------------------------------------------------------
    agent_account = "acct-rot-agent"
    with Session(engine) as s, s.begin():
        agent_uuid = uuid.uuid4()
        s.execute(
            text(
                "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
                "VALUES (:i, :a, :w, 'agent', :a)"
            ),
            {"i": agent_uuid, "a": agent_account, "w": sd.ws},
        )
        old_agent_token = f"svc-rot-agent-{uuid.uuid4().hex[:8]}"
        s.execute(
            text(
                "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) "
                "VALUES (:i, :a, :f, :h)"
            ),
            {
                "i": uuid.uuid4(),
                "a": agent_uuid,
                "f": f"sha256:{agent_account}",
                "h": token_hash(old_agent_token),
            },
        )
    assert _accepts(engine, old_agent_token)
    created.append(_create_task(engine, sd, clock, channel, "rot-agent-before"))

    rotated_at = dt.datetime.now(dt.UTC)
    new_agent = execute_command(
        _runtime(engine, sd, clock),
        sd.principal("admin1"),
        acc.RotateCredential(account_id=agent_account, old_fingerprint=f"sha256:{agent_account}"),
        idempotency_key="rot-agent-1",
        correlation_id="corr-rot-agent",
    )
    new_agent_token = str(new_agent.data["service_token"])
    assert _accepts(engine, new_agent_token)  # the new credential is confirmed first
    assert not _accepts(engine, old_agent_token)
    assert (dt.datetime.now(dt.UTC) - rotated_at).total_seconds() <= ROTATION_BUDGET_S
    created.append(_create_task(engine, sd, clock, channel, "rot-agent-after"))

    # --- 2. and 3. the Mattermost and Telegram bot credentials ---------------------------------
    for provider in ("mattermost", "telegram"):
        old_token = f"bot-{provider}-{uuid.uuid4().hex[:8]}"
        ref = _provider_token_row(engine, sd, provider, old_token)
        assert _provider_accepts(engine, ref, old_token)
        created.append(_create_task(engine, sd, clock, channel, f"rot-{provider}-before"))

        started = dt.datetime.now(dt.UTC)
        new_token = f"bot-{provider}-{uuid.uuid4().hex[:8]}"
        _rotate_provider(engine, ref, new_token)
        assert _provider_accepts(engine, ref, new_token)
        assert not _provider_accepts(engine, ref, old_token)
        assert (dt.datetime.now(dt.UTC) - started).total_seconds() <= ROTATION_BUDGET_S
        created.append(_create_task(engine, sd, clock, channel, f"rot-{provider}-after"))

    # --- 4. an administrator credential ---------------------------------------------------------
    old_admin = sd.tokens["admin2"]
    assert _accepts(engine, old_admin)
    created.append(_create_task(engine, sd, clock, channel, "rot-admin-before"))
    started = dt.datetime.now(dt.UTC)
    rotated = execute_command(
        _runtime(engine, sd, clock),
        sd.principal("admin1"),
        acc.RotateCredential(
            account_id="acct-rot-admin2", old_fingerprint="sha256:acct-rot-admin2"
        ),
        idempotency_key="rot-admin-1",
        correlation_id="corr-rot-admin",
    )
    assert _accepts(engine, str(rotated.data["service_token"]))
    assert not _accepts(engine, old_admin)
    assert (dt.datetime.now(dt.UTC) - started).total_seconds() <= ROTATION_BUDGET_S
    created.append(_create_task(engine, sd, clock, channel, "rot-admin-after"))

    # --- nothing was lost or duplicated across the whole sequence -------------------------------
    assert len(created) == len(set(created)) == 9
    with Session(engine) as s:
        present = s.execute(
            text("SELECT count(*) FROM tasks_projection WHERE task_id = ANY(:ids)"),
            {"ids": created},
        ).scalar_one()
        events = s.execute(
            text(
                "SELECT count(*) FROM events WHERE type = 'TASK_CREATED' "
                "AND aggregate_id = ANY(:ids)"
            ),
            {"ids": created},
        ).scalar_one()
    assert present == 9 and events == 9  # zero loss, zero duplicates
