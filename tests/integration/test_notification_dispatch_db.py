"""Development plan §7G: a command that appends a notifying Event plans its notifications in the
same transaction. Before this wiring the rules engine existed and was tested directly, but nothing
in the command path invoked it, so no notice was ever produced in production."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime, execute_command
from server.api.errors import ApiError
from server.application import tasks as t
from server.application.authz import BusAuthorizer
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import FixedClock
from server.identity.principals import Principal
from server.policy.repository import PostgresPolicyRepository

pytestmark = pytest.mark.db
NOW = dt.datetime(2026, 8, 4, 9, 0, tzinfo=dt.UTC)
CLOCK = FixedClock(NOW)
WS, CHANNEL = uuid.uuid4(), uuid.uuid4()
ACCOUNTS = {
    "creator": ("acct-nd2-creator", uuid.uuid4(), "human"),
    "worker": ("acct-nd2-worker", uuid.uuid4(), "agent"),
    "approver": ("acct-nd2-approver", uuid.uuid4(), "human"),
}
ROLES = {
    "creator": ["task.create", "task.read", "task.delegate", "approval.request"],
    "worker": ["task.read", "task.accept", "task.progress", "task.submit"],
    "approver": ["task.read", "approval.decide", "approval.read"],
}
CRITERIA = ({"statement": "evidence attached", "check_type": "evidence", "required": True},)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-nd2', 'nd2')"),
            {"i": WS},
        )
        s.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name) "
                "VALUES (:i, 'chan-nd2', :w, 'work', 'nd2')"
            ),
            {"i": CHANNEL, "w": WS},
        )
        repo = PostgresPolicyRepository()
        for key, (acct, acc_uuid, typ) in ACCOUNTS.items():
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
            repo.create_role(s, WS, f"nd2-{key}", key)
            repo.commit_role_version(s, f"nd2-{key}", ROLES[key], [], {}, acc_uuid)
            repo.assign_role(s, acc_uuid, f"nd2-{key}", acc_uuid, NOW)
    yield eng
    eng.dispose()


def _principal(key: str) -> Principal:
    acct, acc_uuid, typ = ACCOUNTS[key]
    return Principal(acct, str(acc_uuid), typ, f"sha256:{acct}")


def test_unknown_or_malformed_channel_is_a_stable_client_error(engine: Engine) -> None:
    """An unknown or malformed channel reference is a 404, not a 500: it used to reach the
    projection, where casting it to a UUID raised."""
    from server.application import bus

    rt = Runtime(make_session_factory(engine), BusAuthorizer(), None, CLOCK, str(WS))
    for reference in ("not-a-uuid", "chan-does-not-exist", str(uuid.uuid4())):
        with pytest.raises((bus.CommandError, ApiError)) as exc:
            execute_command(
                rt,
                _principal("creator"),
                t.CreateTask("bad channel", reference, "research", "LOW", criteria=CRITERIA),
                idempotency_key=f"nd2-bad-{reference[:8]}",
                correlation_id="corr-nd2",
            )
        # denied membership or unknown channel: a stable client error, never a 500
        assert exc.value.status in (403, 404), (reference, exc.value.code, exc.value.status)


def test_a_command_plans_its_notifications(engine: Engine) -> None:
    """An approval requested through the command path reaches its eligible approvers."""
    rt = Runtime(make_session_factory(engine), BusAuthorizer(), None, CLOCK, str(WS))
    with Session(engine) as s:
        before = s.execute(
            text("SELECT count(*) FROM notifications WHERE workspace_id = :w"), {"w": WS}
        ).scalar_one()
    task_id = execute_command(
        rt,
        _principal("creator"),
        t.CreateTask("Notify me", str(CHANNEL), "research", "LOW", criteria=CRITERIA),
        idempotency_key="nd2-create",
        correlation_id="corr-nd2",
    ).resource_id
    from server.application.approvals import RequestApproval

    execute_command(
        rt,
        _principal("creator"),
        RequestApproval(
            subject_type="task",
            subject_id=task_id,
            action="tool:task_delegate",
            channel_uuid=str(CHANNEL),
        ),
        idempotency_key="nd2-approval",
        correlation_id="corr-nd2",
    )
    with Session(engine) as s:
        rows = s.execute(
            text(
                "SELECT n.rule_id, n.recipient_account_id, e.type FROM notifications n "
                "JOIN events e ON e.event_id = n.source_event_id "
                "WHERE n.workspace_id = :w ORDER BY n.created_at, n.notification_id"
            ),
            {"w": WS},
        ).all()
    assert len(rows) > before, "requesting an approval must plan a notification"
    assert any(str(r[2]) == "APPROVAL_REQUESTED" for r in rows), rows
    approver = str(ACCOUNTS["approver"][1])
    assert any(str(r[1]) == approver for r in rows), rows
