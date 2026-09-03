"""The `/colab schedule ...` surface the P0-10 grammar advertises (development plan §7A.2, §10A.5):
show, list, pause and resume run through the Command Router with the same permissions as REST, and
an account without `schedule.manage` is refused with the schedule left unchanged."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime, execute_command
from server.application import schedules as sch
from server.application.authz import BusAuthorizer
from server.channels.router import SlashRequest, route
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import FixedClock
from server.identity.principals import Principal
from server.policy.repository import PostgresPolicyRepository
from server.schedules import router_handlers

pytestmark = pytest.mark.db
NOW = dt.datetime(2026, 8, 2, 9, 0, tzinfo=dt.UTC)
CLOCK = FixedClock(NOW)
WS, PI, CHANNEL = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
PI_ID, EXT = "mm:test:slash-sched", "mmchan-slash-sched"
ACCOUNTS: dict[str, tuple[str, uuid.UUID, str]] = {
    "manager": ("acct-slash-mgr", uuid.uuid4(), "mm-slash-mgr"),
    "outsider": ("acct-slash-out", uuid.uuid4(), "mm-slash-out"),
    "runner": ("acct-slash-run", uuid.uuid4(), "mm-slash-run"),
}
ROLES = {
    "manager": ["schedule.manage", "schedule.run", "schedule.read", "task.create", "task.read"],
    "outsider": ["task.read", "schedule.read"],
    "runner": ["task.create", "task.read"],
}


@pytest.fixture(scope="module")
def seeded(database_url: str) -> Iterator[tuple[Engine, str]]:
    router_handlers.register()
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-slash', 'sl')"),
            {"i": WS},
        )
        s.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, "
                "provider, base_url, team_or_bot_ref, identity_display) VALUES (:i, :p, :w, "
                "'mattermost', 'http://mm', 'team-slash', 'prefix')"
            ),
            {"i": PI, "p": PI_ID, "w": WS},
        )
        s.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, provider_instance_id, "
                "external_channel_id, channel_type, display_name) "
                "VALUES (:i, 'chan-slash', :w, :p, :e, 'work', 'slash')"
            ),
            {"i": CHANNEL, "w": WS, "p": PI, "e": EXT},
        )
        repo = PostgresPolicyRepository()
        for key, (acct, acc_uuid, ext) in ACCOUNTS.items():
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) VALUES (:i, :a, :w, 'human', :a)"
                ),
                {"i": acc_uuid, "a": acct, "w": WS},
            )
            s.execute(
                text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                {"c": CHANNEL, "a": acc_uuid},
            )
            repo.create_role(s, WS, f"slash-{key}", key)
            repo.commit_role_version(s, f"slash-{key}", ROLES[key], [], {}, acc_uuid)
            repo.assign_role(s, acc_uuid, f"slash-{key}", acc_uuid, NOW)
            s.execute(
                text(
                    "INSERT INTO external_identity_links (id, link_id, provider_instance_id, "
                    "external_user_id, account_id, verification_method, status, verified_at) "
                    "VALUES (:i, :l, :p, :e, :a, 'admin_approval', 'active', now())"
                ),
                {"i": uuid.uuid4(), "l": f"link-slash-{key}", "p": PI, "e": ext, "a": acc_uuid},
            )
    rt = Runtime(make_session_factory(eng), BusAuthorizer(), None, CLOCK, str(WS))
    created = execute_command(
        rt,
        _principal("manager"),
        sch.CreateSchedule(
            name="slash nightly",
            cron_expression="0 9 * * *",
            timezone="UTC",
            channel_id="chan-slash",
            execution_principal_id="acct-slash-run",
            agent_selection={"mode": "capability", "required_capabilities": ["cap-slash"]},
            action_template={
                "schema_id": "action-template.v1",
                "action": "task_create",
                "input": {"title": "slash task", "domain": "research", "risk": "LOW"},
            },
        ),
        idempotency_key="slash-create-1",
        correlation_id="corr-slash",
    )
    schedule_id = str(created.resource_id)
    execute_command(
        rt,
        _principal("manager"),
        sch.EnableSchedule(schedule_id=schedule_id),
        idempotency_key="slash-enable-1",
        correlation_id="corr-slash",
    )
    yield eng, schedule_id
    eng.dispose()


def _principal(key: str) -> Principal:
    acct, acc_uuid, _ = ACCOUNTS[key]
    return Principal(acct, str(acc_uuid), "human", f"sha256:{acct}")


def _req(text_in: str, user: str) -> SlashRequest:
    return SlashRequest(
        provider_instance_id=PI_ID,
        team_id="team-slash",
        channel_id=EXT,
        user_id=ACCOUNTS[user][2],
        user_name=user,
        command="/colab",
        text=text_in,
        trigger_id=uuid.uuid4().hex,
        response_url=None,
        post_id=None,
        root_id=None,
    )


def _status(engine: Engine, schedule_id: str) -> str:
    with Session(engine) as s:
        return str(
            s.execute(
                text("SELECT status FROM schedules WHERE schedule_id = :s"), {"s": schedule_id}
            ).scalar_one()
        )


def _runtime(engine: Engine) -> Any:
    return Runtime(make_session_factory(engine), BusAuthorizer(), None, CLOCK, str(WS))


def test_schedule_slash_commands_work_and_respect_permissions(
    seeded: tuple[Engine, str],
) -> None:
    engine, schedule_id = seeded
    rt = _runtime(engine)

    shown = route(rt, _req(f"schedule show {schedule_id}", "manager"), CLOCK)
    assert shown.code == "OK" and schedule_id in shown.text, shown
    listed = route(rt, _req("schedule list", "manager"), CLOCK)
    assert listed.code == "OK" and schedule_id in listed.text, listed

    paused = route(rt, _req(f"schedule pause {schedule_id}", "manager"), CLOCK)
    assert paused.code == "OK", paused
    assert _status(engine, schedule_id) == "PAUSED"
    resumed = route(rt, _req(f"schedule resume {schedule_id}", "manager"), CLOCK)
    assert resumed.code == "OK", resumed
    assert _status(engine, schedule_id) == "ENABLED"

    denied = route(rt, _req(f"schedule pause {schedule_id}", "outsider"), CLOCK)
    assert denied.code != "OK", denied
    assert _status(engine, schedule_id) == "ENABLED"  # a refused command changes nothing
