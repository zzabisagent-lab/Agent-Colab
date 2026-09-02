"""V-P2-26 (interactive actions) and V-P2-28 (Agent identity display) against PostgreSQL."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime, execute_command
from server.application import approvals as approvals_app
from server.application import tasks as tasks_app
from server.application.authz import BusAuthorizer
from server.channels.actions import (
    ActionContext,
    ActionError,
    ActionHandler,
    ActionRequest,
)
from server.channels.mattermost.client import FakeMattermostClient
from server.channels.mattermost.delivery import MattermostChannelProvider
from server.channels.mattermost.provider import load_instance
from server.config import Settings
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import FixedClock
from server.identity.principals import Principal, token_hash
from server.main import create_app
from server.policy.repository import PostgresPolicyRepository

pytestmark = pytest.mark.db

WS = uuid.uuid4()
PI = uuid.uuid4()
PI_ID = "mm:test:actions"
CHANNEL = uuid.uuid4()
EXT = "mmchan-actions-1"
SECRET = b"integration-action-secret"
CLOCK = FixedClock(dt.datetime(2026, 7, 1, 9, 0, tzinfo=dt.UTC))
ACCOUNTS: dict[str, tuple[str, uuid.UUID, str, str]] = {
    "creator": ("acct-act-creator", uuid.uuid4(), "human", "mm-act-creator"),
    "assignee": ("acct-act-assignee", uuid.uuid4(), "agent", "mm-act-assignee"),
    "nobody": ("acct-act-nobody", uuid.uuid4(), "human", "mm-act-nobody"),
    "approver": ("acct-act-approver", uuid.uuid4(), "human", "mm-act-approver"),
}
ROLES = {
    "creator": ["task.create", "task.read", "task.delegate", "task.cancel", "approval.request"],
    "assignee": ["task.read", "task.accept", "task.progress", "task.submit"],
    "nobody": ["task.read"],
    "approver": ["task.read", "approval.decide", "approval.read"],
}
CRITERIA = ({"statement": "done", "check_type": "evidence", "required": True},)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-act', 'act')"),
            {"i": WS},
        )
        s.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, provider, "
                "base_url, team_or_bot_ref, identity_display, config) VALUES (:i, :p, :w, "
                "'mattermost', 'http://mm', 'team-act', 'prefix', "
                '\'{"team_name": "colab-test"}\')'
            ),
            {"i": PI, "p": PI_ID, "w": WS},
        )
        s.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, provider_instance_id, "
                "external_channel_id, channel_type, display_name) "
                "VALUES (:i, 'chan-act', :w, :p, :e, 'work', 'act')"
            ),
            {"i": CHANNEL, "w": WS, "p": PI, "e": EXT},
        )
        repo = PostgresPolicyRepository()
        for key, (acct, acc_uuid, typ, ext) in ACCOUNTS.items():
            s.execute(
                text(
                    "INSERT INTO accounts "
                    "(id, account_id, workspace_id, account_type, display_name) "
                    "VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc_uuid, "a": acct, "w": WS, "t": typ},
            )
            s.execute(
                text(
                    "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) "
                    "VALUES (:i, :a, :f, :h)"
                ),
                {
                    "i": uuid.uuid4(),
                    "a": acc_uuid,
                    "f": f"sha256:{acct}",
                    "h": token_hash(f"tok-{key}"),
                },
            )
            s.execute(
                text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                {"c": CHANNEL, "a": acc_uuid},
            )
            repo.create_role(s, WS, f"act-{key}", key)
            repo.commit_role_version(
                s, f"act-{key}", ROLES[key], [], {"max_risk": "MEDIUM"}, acc_uuid
            )
            repo.assign_role(s, acc_uuid, f"act-{key}", acc_uuid, CLOCK.now())
            if True:  # every seeded user is linked; the unlinked case uses an unknown user id
                s.execute(
                    text(
                        "INSERT INTO external_identity_links (id, link_id, provider_instance_id, "
                        "external_user_id, account_id, verification_method, status, verified_at) "
                        "VALUES (:i, :l, :p, :e, :a, 'admin_approval', 'active', now())"
                    ),
                    {"i": uuid.uuid4(), "l": f"link-act-{key}", "p": PI, "e": ext, "a": acc_uuid},
                )
    yield eng
    eng.dispose()


def _principal(key: str) -> Principal:
    acct, acc_uuid, typ, _ = ACCOUNTS[key]
    return Principal(acct, str(acc_uuid), typ, f"sha256:{acct}")


def _runtime(engine: Engine) -> Runtime:
    return Runtime(make_session_factory(engine), BusAuthorizer(), None, CLOCK, str(WS))


def _events(engine: Engine, task_id: str) -> list[str]:
    with Session(engine) as s:
        return [
            r[0]
            for r in s.execute(
                text("SELECT type FROM events WHERE task_id = :t ORDER BY recorded_seq"),
                {"t": task_id},
            ).all()
        ]


def _audits(engine: Engine, action: str) -> int:
    with Session(engine) as s:
        return int(
            s.execute(
                text("SELECT count(*) FROM audit_events WHERE action = :a AND workspace_id = :w"),
                {"a": action, "w": WS},
            ).scalar_one()
        )


def _request(user: str, ctx: dict[str, Any], post_id: str = "post-card-1") -> ActionRequest:
    return ActionRequest(PI_ID, user, EXT, post_id, ctx, trigger_id=f"trig-{uuid.uuid4().hex[:8]}")


def _signed(
    action: str, subject_id: str, subject_type: str = "task", nonce: str | None = None
) -> dict[str, Any]:
    ctx = ActionContext(
        subject_type, subject_id, action, int(CLOCK.now().timestamp()), nonce or uuid.uuid4().hex
    )
    return ctx.as_button_context(SECRET)


def test_interactive_actions_execute_exactly_once_and_reject_everything_else(
    engine: Engine,
) -> None:
    rt = _runtime(engine)
    handler = ActionHandler(rt, CLOCK, SECRET)
    creator = _principal("creator")
    task_id = execute_command(
        rt,
        creator,
        tasks_app.CreateTask("Button task", str(CHANNEL), "research", "LOW", criteria=CRITERIA),
        idempotency_key="act-create",
        correlation_id="act",
    ).resource_id
    execute_command(
        rt,
        creator,
        tasks_app.DelegateTask(task_id, "acct-act-assignee"),
        idempotency_key="act-del",
        correlation_id="act",
    )
    base = _events(engine, task_id)

    # tampered signature: 401, zero domain Events, audited
    bad = _signed("accept", task_id)
    bad["signature"] = "0" * 64
    with pytest.raises(ActionError) as exc:
        handler.handle(_request(ACCOUNTS["assignee"][3], bad))
    assert exc.value.status == 401 and exc.value.code == "CALLBACK_SIGNATURE_INVALID"
    assert _events(engine, task_id) == base and _audits(engine, "action.rejected") >= 1

    # unlinked Mattermost user: guidance only, zero Events
    unlinked = handler.handle(_request("mm-unknown-user", _signed("accept", task_id)))
    assert unlinked.code == "EXTERNAL_IDENTITY_NOT_ACTIVE" and not unlinked.executed
    assert _events(engine, task_id) == base and _audits(engine, "action.unlinked") == 1

    # linked but unauthorized user: rejected (normalized) + audited, zero Events
    denied = handler.handle(_request(ACCOUNTS["nobody"][3], _signed("accept", task_id)))
    assert denied.code != "OK" and not denied.executed
    assert _events(engine, task_id) == base and _audits(engine, "action.denied") == 1

    # authorized click executes once; the duplicate click (same nonce) replays without a new Event
    ctx = _signed("accept", task_id, nonce="click-nonce-1")
    first = handler.handle(_request(ACCOUNTS["assignee"][3], ctx))
    assert first.executed and first.code == "OK" and not first.replayed
    assert _events(engine, task_id) == [*base, "TASK_ACCEPTED"]
    again = handler.handle(_request(ACCOUNTS["assignee"][3], ctx))
    assert again.replayed and again.event_id == first.event_id
    assert _events(engine, task_id) == [*base, "TASK_ACCEPTED"]

    # a fresh click after acceptance is a transition error, not a second acceptance
    later = handler.handle(_request(ACCOUNTS["assignee"][3], _signed("accept", task_id)))
    assert later.code == "TASK_TRANSITION_INVALID" and not later.executed
    assert _events(engine, task_id) == [*base, "TASK_ACCEPTED"]

    # submit button never executes: it explains the evidence requirement
    sub = handler.handle(_request(ACCOUNTS["assignee"][3], _signed("submit", task_id)))
    assert sub.code == "EVIDENCE_REQUIRED" and "/colab task submit" in sub.ephemeral_text

    # HIGH approval: button shows web-console guidance and does not approve
    apr = execute_command(
        rt,
        creator,
        approvals_app.RequestApproval("task", task_id, "external_send"),
        idempotency_key="act-apr",
        correlation_id="act",
    ).resource_id
    high = handler.handle(_request(ACCOUNTS["approver"][3], _signed("approve", apr, "approval")))
    assert high.code == "REAUTH_REQUIRED" and not high.executed
    with Session(engine) as s:
        assert (
            s.execute(
                text("SELECT status FROM approval_grants WHERE approval_id = :a"), {"a": apr}
            ).scalar_one()
            == "PENDING"
        )


def test_actions_endpoint_rejects_tampered_callbacks(
    database_url: str, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_COLAB_MATTERMOST_ACTION_SECRET", SECRET.decode())
    app = create_app(Settings(database_url=database_url, base_url="http://test"))
    from server.api.v1.providers_mattermost_actions import router as actions_router

    app.include_router(actions_router)  # the parent mounts it before the MCP root mount
    app.router.routes.insert(0, app.router.routes.pop())
    with TestClient(app) as c, Session(engine) as s:
        n_before = s.execute(text("SELECT count(*) FROM events")).scalar_one()
        ctx = _signed("accept", "task-x")
        ctx["signature"] = "f" * 64
        r = c.post(
            "/api/v1/providers/mattermost/actions",
            json={
                "user_id": "mm-act-assignee",
                "channel_id": EXT,
                "post_id": "p",
                "team_id": "team-act",
                "context": ctx,
            },
        )
        assert r.status_code == 401 and r.json()["code"] == "CALLBACK_SIGNATURE_INVALID"
        assert s.execute(text("SELECT count(*) FROM events")).scalar_one() == n_before


def test_agent_identity_display_and_injection_audit(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with Session(engine) as s:
        prefix_inst = load_instance(s, PI_ID)
        assert prefix_inst is not None
    client = FakeMattermostClient()
    provider = MattermostChannelProvider(client, prefix_inst, factory)
    post_id = provider.deliver(
        f"mattermost:{EXT}",
        {
            "message": "result ready",
            "agent_display_name": "Research Agent",
            "dedupe_key": "id-1",
            "props": {"override_username": "system-admin"},
        },
    )
    assert client.posts[post_id].message == "[Research Agent] result ready"
    assert "override_username" not in client.posts[post_id].props
    assert _audits(engine, "agent.identity_injection_ignored") == 1
    with Session(engine) as s, s.begin():
        s.execute(
            text("UPDATE provider_instances SET identity_display = 'override' WHERE id = :i"),
            {"i": PI},
        )
    with Session(engine) as s:
        override_inst = load_instance(s, PI_ID)
        assert override_inst is not None
    provider2 = MattermostChannelProvider(client, override_inst, factory)
    post2 = provider2.deliver(
        f"mattermost:{EXT}",
        {"message": "hello", "agent_display_name": "Research Agent", "dedupe_key": "id-2"},
    )
    assert client.posts[post2].message == "hello"
    assert client.posts[post2].props["override_username"] == "Research Agent"
