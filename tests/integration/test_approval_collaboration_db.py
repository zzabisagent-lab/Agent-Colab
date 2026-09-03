"""P6-01 approval collaboration: V-P6-29 (card buttons decide LOW/MEDIUM, HIGH shows web guidance,
self-approval is rejected and audited), V-P6-01 (an approval cannot be reused for another subject
or action), V-P6-02 (a high-risk action without an approval performs zero execution) and V-P6-22
(an expired approval executes nothing, is EXPIRED and is not reusable)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime, execute_command
from server.application import bus
from server.application.approvals import (
    ConsumeApproval,
    DecideApproval,
    ExpireApprovals,
    RequestApproval,
)
from server.application.authz import BusAuthorizer
from server.channels.actions import ActionContext, ActionHandler, ActionRequest
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore
from server.identity.principals import Principal
from server.policy.authorization import Authorizer
from server.policy.repository import PostgresPolicyRepository

pytestmark = pytest.mark.db
NOW = dt.datetime(2026, 8, 1, 9, 0, tzinfo=dt.UTC)
CLOCK = FixedClock(NOW)
SECRET = b"approval-collaboration-secret"
WS, PI, CHANNEL = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
PI_ID, EXT = "mm:test:approvals6", "mmchan-approvals-6"
ACCOUNTS: dict[str, tuple[str, uuid.UUID, str, str]] = {
    "requester": ("acct-ap6-req", uuid.uuid4(), "human", "mm-ap6-req"),
    "approver": ("acct-ap6-app", uuid.uuid4(), "human", "mm-ap6-app"),
    "agent": ("acct-ap6-agent", uuid.uuid4(), "agent", "mm-ap6-agent"),
}
ROLES = {
    "requester": ["task.read", "approval.request", "approval.read"],
    "approver": ["task.read", "approval.decide", "approval.read", "approval.revoke"],
    "agent": ["task.read", "task.delegate"],
}


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-ap6', 'ap6')"),
            {"i": WS},
        )
        s.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, "
                "provider, base_url, team_or_bot_ref, identity_display) VALUES (:i, :p, :w, "
                "'mattermost', 'http://mm', 'team-ap6', 'prefix')"
            ),
            {"i": PI, "p": PI_ID, "w": WS},
        )
        s.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, provider_instance_id, "
                "external_channel_id, channel_type, display_name) "
                "VALUES (:i, 'chan-ap6', :w, :p, :e, 'work', 'ap6')"
            ),
            {"i": CHANNEL, "w": WS, "p": PI, "e": EXT},
        )
        repo = PostgresPolicyRepository()
        for key, (acct, acc_uuid, typ, ext) in ACCOUNTS.items():
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc_uuid, "a": acct, "w": WS, "t": typ},
            )
            s.execute(
                text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                {"c": CHANNEL, "a": acc_uuid},
            )
            repo.create_role(s, WS, f"ap6-{key}", key)
            repo.commit_role_version(
                s,
                f"ap6-{key}",
                ROLES[key],
                [],
                {"max_risk": "CRITICAL" if key == "approver" else "MEDIUM"},
                acc_uuid,
            )
            repo.assign_role(s, acc_uuid, f"ap6-{key}", acc_uuid, NOW)
            s.execute(
                text(
                    "INSERT INTO external_identity_links (id, link_id, provider_instance_id, "
                    "external_user_id, account_id, verification_method, status, verified_at) "
                    "VALUES (:i, :l, :p, :e, :a, 'admin_approval', 'active', now())"
                ),
                {"i": uuid.uuid4(), "l": f"link-ap6-{key}", "p": PI, "e": ext, "a": acc_uuid},
            )
        s.execute(
            text(
                "INSERT INTO tasks_projection (task_id, workspace_id, root_task_id, channel_id, "
                "title, domain, risk, status, created_at, updated_at) VALUES "
                "('task-ap6-1', :w, 'task-ap6-1', :c, 'approval subject', 'research', 'LOW', "
                "'OPEN', :n, :n)"
            ),
            {"w": WS, "c": CHANNEL, "n": NOW},
        )
    yield eng
    eng.dispose()


def _principal(key: str) -> Principal:
    acct, acc_uuid, typ, _ = ACCOUNTS[key]
    return Principal(acct, str(acc_uuid), typ, f"sha256:{acct}")


def _runtime(engine: Engine) -> Runtime:
    return Runtime(make_session_factory(engine), BusAuthorizer(), None, CLOCK, str(WS))


def _ctx(session: Session, who: str, key: str) -> bus.CommandContext:
    return bus.CommandContext(
        session=session,
        store=PostgresEventStore(session, clock=CLOCK),
        authorizer=BusAuthorizer(),
        clock=CLOCK,
        principal=bus.Principal(
            _principal(who).account_id,
            _principal(who).account_uuid,
            _principal(who).account_type,
            _principal(who).credential_fingerprint,
        ),
        workspace_id=str(WS),
        correlation_id=f"corr-{key}",
        idempotency_key=key,
        extras={"policy_authorizer": Authorizer(PostgresPolicyRepository(), clock=CLOCK)},
    )


def _request_approval(engine: Engine, key: str, **over: Any) -> str:
    fields: dict[str, Any] = {
        "subject_type": "task",
        "subject_id": "task-ap6-1",
        "action": "tool:task_delegate",
        "channel_uuid": str(CHANNEL),
    }
    fields.update(over)
    return str(_dispatch(engine, "requester", RequestApproval(**fields), key).resource_id)


def _dispatch(engine: Engine, who: str, command: Any, key: str) -> Any:
    """The production path: dispatch runs the renderer hook that posts the approval card."""
    return execute_command(
        _runtime(engine), _principal(who), command, idempotency_key=key, correlation_id=f"c-{key}"
    )


def _card(engine: Engine, approval_id: str) -> dict[str, Any] | None:
    with Session(engine) as s:
        row = s.execute(
            text(
                "SELECT payload FROM delivery_outbox WHERE workspace_id = :w "
                "AND dedupe_key LIKE :k ORDER BY id"
            ),
            {"w": WS, "k": f"approvalcard:%:{approval_id}%"},
        ).first()
    return None if row is None else dict(row[0])


def _press(engine: Engine, who: str, action: str, approval_id: str) -> Any:
    handler = ActionHandler(_runtime(engine), CLOCK, SECRET)
    ctx = ActionContext(
        "approval", approval_id, action, int(CLOCK.now().timestamp()), uuid.uuid4().hex
    )
    request = ActionRequest(
        PI_ID,
        ACCOUNTS[who][3],
        EXT,
        "post-ap6",
        ctx.as_button_context(SECRET),
        trigger_id=f"trig-{uuid.uuid4().hex[:8]}",
    )
    return handler.handle(request)


def _status(engine: Engine, approval_id: str) -> str:
    with Session(engine) as s:
        return str(
            s.execute(
                text("SELECT status FROM approval_grants WHERE approval_id = :a"),
                {"a": approval_id},
            ).scalar_one()
        )


def _audits(engine: Engine, action: str) -> int:
    with Session(engine) as s:
        return int(
            s.execute(
                text("SELECT count(*) FROM audit_events WHERE action = :a AND workspace_id = :w"),
                {"a": action, "w": WS},
            ).scalar_one()
        )


def test_low_risk_button_decides_and_high_risk_sends_to_the_web_console(engine: Engine) -> None:
    """V-P6-29: LOW/MEDIUM approve from the card; HIGH shows guidance and never approves; a
    requester approving their own request is rejected and audited."""
    low = _request_approval(engine, "ap6-low-1")  # tool:task_delegate is MEDIUM risk
    card = _card(engine, low)
    assert card is not None and "approve" in str(card["props"]["buttons"])
    assert "Approval" in card["message"] and "risk MEDIUM" in card["message"]

    before = _audits(engine, "policy.deny")
    self_press = _press(engine, "requester", "approve", low)
    assert not self_press.executed and self_press.code != "OK", self_press
    assert _status(engine, low) == "PENDING"  # the requester cannot approve their own request
    assert _audits(engine, "policy.deny") + _audits(engine, "action.denied") > before

    ok = _press(engine, "approver", "approve", low)
    assert ok.executed and ok.code == "OK" and ok.event_id, ok
    assert _status(engine, low) == "APPROVED"

    high = _request_approval(engine, "ap6-high-1", action="api:secret_grant_scope_expand")
    high_card = _card(engine, high)
    assert high_card is not None and high_card["props"]["buttons"] == []
    assert "web console" in high_card["message"] and "MFA" in high_card["message"]
    guided = _press(engine, "approver", "approve", high)
    assert not guided.executed and "web console" in guided.ephemeral_text, guided
    assert _status(engine, high) == "PENDING"  # a button never approves HIGH or CRITICAL


def test_approval_cannot_be_reused_for_another_subject_or_action(engine: Engine) -> None:
    """V-P6-01: consuming an approval outside its subject/action scope is rejected."""
    approval = _request_approval(engine, "ap6-scope-1")
    _dispatch(engine, "approver", DecideApproval(approval, "APPROVE"), "ap6-scope-dec")
    with Session(engine) as s, s.begin(), pytest.raises(bus.CommandError) as other_subject:
        bus.execute(
            ConsumeApproval(approval, "k-other-subject", "task", "task-ap6-OTHER"),
            _ctx(s, "agent", "ap6-scope-c1"),
        )
    assert "SCOPE" in other_subject.value.code or "MISMATCH" in other_subject.value.code
    with Session(engine) as s:
        assert (
            s.execute(
                text("SELECT count(*) FROM approval_consumptions WHERE approval_id = :a"),
                {"a": approval},
            ).scalar_one()
            == 0
        )


def test_high_risk_action_without_an_approval_executes_nothing(engine: Engine) -> None:
    """V-P6-02: a high-risk action attempted without an approval performs zero execution."""
    with Session(engine) as s:
        before = s.execute(
            text("SELECT count(*) FROM events WHERE workspace_id = :w"), {"w": WS}
        ).scalar_one()
    missing = "apr-does-not-exist"
    with Session(engine) as s, s.begin(), pytest.raises(bus.CommandError) as exc:
        bus.execute(
            ConsumeApproval(missing, "k-missing", "task", "task-ap6-1"),
            _ctx(s, "agent", "ap6-none-1"),
        )
    assert exc.value.code in ("APPROVAL_NOT_FOUND", "NOT_FOUND")
    with Session(engine) as s:
        after = s.execute(
            text("SELECT count(*) FROM events WHERE workspace_id = :w"), {"w": WS}
        ).scalar_one()
    assert after == before


def test_expired_approval_executes_nothing_and_cannot_be_reused(engine: Engine) -> None:
    """V-P6-22: after expiry the state is EXPIRED, consumption fails and no Event is appended."""
    approval = _request_approval(engine, "ap6-exp-1")
    _dispatch(engine, "approver", DecideApproval(approval, "APPROVE"), "ap6-exp-dec")
    with Session(engine) as s:
        expires = s.execute(
            text("SELECT expires_at FROM approval_grants WHERE approval_id = :a"), {"a": approval}
        ).scalar_one()
    CLOCK.advance(expires + dt.timedelta(minutes=1) - CLOCK.now())  # past validity, no row edits
    _dispatch(engine, "approver", ExpireApprovals(), "ap6-exp-sweep")
    assert _status(engine, approval) == "EXPIRED"
    with Session(engine) as s:
        before = s.execute(
            text("SELECT count(*) FROM events WHERE aggregate_id = :a"), {"a": approval}
        ).scalar_one()
    with Session(engine) as s, s.begin(), pytest.raises(bus.CommandError) as exc:
        bus.execute(
            ConsumeApproval(approval, "k-expired", "task", "task-ap6-1"),
            _ctx(s, "agent", "ap6-exp-consume"),
        )
    assert exc.value.code in ("APPROVAL_NOT_USABLE", "APPROVAL_EXPIRED"), exc.value.code
    with Session(engine) as s:
        after = s.execute(
            text("SELECT count(*) FROM events WHERE aggregate_id = :a"), {"a": approval}
        ).scalar_one()
        consumed = s.execute(
            text("SELECT count(*) FROM approval_consumptions WHERE approval_id = :a"),
            {"a": approval},
        ).scalar_one()
    assert after == before and consumed == 0
