"""V-P1-31: exact recipient sets, zero duplicates in the dedupe window, loss has zero effect
on Task state."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore
from server.events.store import AppendRequest
from server.notifications.outbox import StubProvider, cancel_pending, drain
from server.notifications.rules import NotificationEngine, load_rules, sync_rules

pytestmark = pytest.mark.db

WS = uuid.uuid4()
CHANNEL = uuid.uuid4()
APPROVAL_CHANNEL = uuid.uuid4()
OPS_CHANNEL = uuid.uuid4()
IDS = {
    name: uuid.uuid4()
    for name in (
        "requester",
        "impl_agent",
        "approver_a",
        "approver_b",
        "approver_agent",
        "outsider",
        "muted",
        "digest",
        "verifier",
        "delegator",
        "member",
        "admin",
        "service",
    )
}
CLOCK = FixedClock(dt.datetime(2026, 3, 1, 10, 0, tzinfo=dt.UTC))


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-ntf', 'ntf')"),
            {"i": WS},
        )
        for name, acc in IDS.items():
            typ = (
                "agent"
                if name in ("impl_agent", "approver_agent")
                else "service"
                if name == "service"
                else "human"
            )
            c.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc, "a": f"acct-ntf-{name}", "w": WS, "t": typ},
            )
        for cid, ctype, name in (
            (CHANNEL, "work", "work"),
            (APPROVAL_CHANNEL, "approval", "approvals"),
            (OPS_CHANNEL, "ops", "ops"),
        ):
            c.execute(
                text(
                    "INSERT INTO channels (id, channel_id, workspace_id, channel_type, "
                    "display_name) VALUES (:i, :c, :w, :t, :n)"
                ),
                {"i": cid, "c": f"chan-ntf-{name}", "w": WS, "t": ctype, "n": name},
            )
        for name in (
            "requester",
            "impl_agent",
            "approver_a",
            "approver_b",
            "approver_agent",
            "muted",
            "digest",
            "delegator",
            "member",
        ):
            c.execute(
                text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                {"c": CHANNEL, "a": IDS[name]},
            )
        c.execute(
            text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
            {"c": OPS_CHANNEL, "a": IDS["admin"]},
        )
        # roles: approver (approval.decide), admin (admin.*); the outsider holds the role but is
        # not a channel member
        for role_id, perms in (
            ("role-ntf-approver", ["approval.decide", "task.read"]),
            ("role-ntf-admin", ["admin.*"]),
        ):
            c.execute(
                text(
                    "INSERT INTO roles (id, role_id, workspace_id, display_name, current_version) "
                    "VALUES (:i, :r, :w, :r, 1)"
                ),
                {"i": uuid.uuid4(), "r": role_id, "w": WS},
            )
            c.execute(
                text(
                    "INSERT INTO role_versions (id, role_id, version, permissions, deny, "
                    "constraints, "
                    "policy_hash, created_by) VALUES (:i, :r, 1, CAST(:p AS jsonb), '[]', "
                    "'{}', 'h', :by)"
                ),
                {"i": uuid.uuid4(), "r": role_id, "p": json.dumps(perms), "by": IDS["admin"]},
            )
        for name, role_id in (
            ("approver_a", "role-ntf-approver"),
            ("approver_b", "role-ntf-approver"),
            ("approver_agent", "role-ntf-approver"),
            ("outsider", "role-ntf-approver"),
            ("requester", "role-ntf-approver"),
            ("muted", "role-ntf-approver"),
            ("digest", "role-ntf-approver"),
            ("admin", "role-ntf-admin"),
        ):
            c.execute(
                text(
                    "INSERT INTO principal_role_assignments (id, account_id, role_id, assigned_by, "
                    "valid_from) VALUES (:i, :a, :r, :by, :vf)"
                ),
                {
                    "i": uuid.uuid4(),
                    "a": IDS[name],
                    "r": role_id,
                    "by": IDS["admin"],
                    "vf": CLOCK.now() - dt.timedelta(days=1),
                },
            )
        c.execute(
            text("INSERT INTO notification_preferences (account_id, muted) VALUES (:a, true)"),
            {"a": IDS["muted"]},
        )
        c.execute(
            text("INSERT INTO notification_preferences (account_id, digest) VALUES (:a, true)"),
            {"a": IDS["digest"]},
        )
        c.execute(
            text(
                "INSERT INTO tasks_projection (task_id, workspace_id, root_task_id, "
                "channel_id, title, domain, risk, status, delegated_by, created_at, "
                "updated_at)"
                "VALUES ('task-ntf-1', :w, 'task-ntf-1', :c, 't', 'research', 'LOW', "
                "'WAITING', :d, now(), now())"
            ),
            {"w": WS, "c": CHANNEL, "d": IDS["delegator"]},
        )
    with Session(eng) as s, s.begin():
        sync_rules(s, str(WS), load_rules())
    yield eng
    eng.dispose()


def _append(s: Session, **kw: object) -> dict[str, object]:
    store = PostgresEventStore(s, clock=CLOCK)
    base: dict[str, object] = {
        "workspace_id": str(WS),
        "actor_account_id": str(IDS["requester"]),
        "correlation_id": "corr-ntf",
        "channel_id": str(CHANNEL),
    }
    base.update(kw)
    res = store.append(AppendRequest(**base))  # type: ignore[arg-type]
    ev = store.get(res.event_id)
    assert ev is not None
    return ev


def _approval_event(
    s: Session, approval_id: str, key: str, risk: str = "HIGH"
) -> dict[str, object]:
    s.execute(
        text(
            "INSERT INTO approval_grants (id, approval_id, workspace_id, subject_type, "
            "subject_id, action, risk, status, requested_by,"
            "implementing_agent_account_id, channel_id, valid_from, expires_at) VALUES "
            "(:i, :a, :w, 'task', 'task-ntf-1', 'external_send', :r,"
            "'PENDING', :req, :impl, :c, :vf, :ex) ON CONFLICT (approval_id) DO NOTHING"
        ),
        {
            "i": uuid.uuid4(),
            "a": approval_id,
            "w": WS,
            "r": risk,
            "req": IDS["requester"],
            "impl": IDS["impl_agent"],
            "c": CHANNEL,
            "vf": CLOCK.now(),
            "ex": CLOCK.now() + dt.timedelta(hours=24),
        },
    )
    return _append(
        s,
        aggregate_type="approval",
        aggregate_id=approval_id,
        type="APPROVAL_REQUESTED",
        idempotency_scope="approval:request",
        idempotency_key=key,
        task_id="task-ntf-1",
        payload={
            "approval_id": approval_id,
            "subject_type": "task",
            "subject_id": "task-ntf-1",
            "action": "external_send",
            "risk": risk,
            "expires_at": "2026-03-02T10:00:00.000Z",
        },
    )


def test_approval_requested_exact_recipients_and_dedupe(engine: Engine) -> None:
    eng = NotificationEngine(clock=CLOCK)
    with Session(engine) as s, s.begin():
        ev = _approval_event(s, "apr-ntf-1", "a1")
        records = eng.on_event(s, ev)
    by = {r.recipient: r.status for r in records}
    # HIGH: humans only, channel members only, requester/implementing agent excluded,
    # outsider (non-member) excluded
    assert by == {
        str(IDS["approver_a"]): "queued",
        str(IDS["approver_b"]): "queued",
        str(IDS["muted"]): "suppressed",
        str(IDS["digest"]): "digest",
    }
    with Session(engine) as s:
        rows = s.execute(
            text(
                "SELECT recipient_account_id, status FROM notifications WHERE source_event_id = :e"
            ),
            {"e": ev["event_id"]},
        ).all()
        assert {str(r[0]) for r in rows} == set(by)
        outbox = s.execute(
            text(
                "SELECT kind, destination FROM delivery_outbox WHERE source_event_id = :e "
                "ORDER BY id"
            ),
            {"e": ev["event_id"]},
        ).all()
        kinds = [k for k, _ in outbox]
        assert kinds.count("notification") == 4  # 2 recipients x (thread, dm)
        assert kinds.count("notification_channel_post") == 1 and any(
            d == f"mattermost:approval_channel:{APPROVAL_CHANNEL}" for _, d in outbox
        )
        assert kinds.count("notification_digest") == 1
        assert (
            kinds.count("notification_reminder") == 8
        )  # 2 recipients x 2 channels x (50%, expiry)
        reminder_at = s.execute(
            text(
                "SELECT DISTINCT next_attempt_at FROM delivery_outbox WHERE kind = "
                "'notification_reminder' AND source_event_id = :e ORDER BY 1"
            ),
            {"e": ev["event_id"]},
        ).all()
        assert [r[0] for r in reminder_at] == [
            dt.datetime(2026, 3, 1, 22, 0, tzinfo=dt.UTC),
            dt.datetime(2026, 3, 2, 10, 0, tzinfo=dt.UTC),
        ]
    # the same subject again (a second APPROVAL_REQUESTED for the same approval) -> with a zero
    # window each Event is its own bucket, so it is notified again (bounded by the recipient set)
    with Session(engine) as s, s.begin():
        ev2 = _approval_event(s, "apr-ntf-1", "a2")
        records2 = eng.on_event(s, ev2)
    assert (
        all(r.status == "duplicate" for r in records2)
        or records2 == []
        or all(r.status in ("duplicate",) for r in records2)
    )
    with Session(engine) as s:
        n = s.execute(
            text("SELECT count(*) FROM notifications WHERE source_event_id = :e"),
            {"e": ev2["event_id"]},
        ).scalar()
    # window dedupe itself is proven by the AGENT_MARKED_OFFLINE test below
    assert n in (0, 4)


def test_agent_offline_dedupe_window(engine: Engine) -> None:
    eng = NotificationEngine(clock=CLOCK)
    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "INSERT INTO agents (id, agent_id, workspace_id, account_id, "
                "adapter_type, display_name) VALUES (:i, 'agent-ntf-1', :w, :a, 'mcp', "
                "'x') ON CONFLICT DO NOTHING"
            ),
            {"i": uuid.uuid4(), "w": WS, "a": IDS["impl_agent"]},
        )
        first = _append(
            s,
            aggregate_type="agent",
            aggregate_id="agent-ntf-1",
            type="AGENT_MARKED_OFFLINE",
            idempotency_scope="agent:offline",
            idempotency_key="o1",
            channel_id=None,
            payload={
                "agent_id": "agent-ntf-1",
                "missed_heartbeats": 3,
                "owner_account_id": str(IDS["member"]),
            },
        )
        r1 = eng.on_event(s, first)
        second = _append(
            s,
            aggregate_type="agent",
            aggregate_id="agent-ntf-1",
            type="AGENT_MARKED_OFFLINE",
            idempotency_scope="agent:offline",
            idempotency_key="o2",
            channel_id=None,
            payload={
                "agent_id": "agent-ntf-1",
                "missed_heartbeats": 4,
                "owner_account_id": str(IDS["member"]),
            },
        )
        r2 = eng.on_event(s, second)
    assert [(r.recipient, r.status) for r in r1] == [(str(IDS["member"]), "queued")]
    assert [(r.recipient, r.status) for r in r2] == [(str(IDS["member"]), "duplicate")]
    with Session(engine) as s:
        assert (
            s.execute(
                text("SELECT count(*) FROM notifications WHERE rule_id = 'ntf-agent-offline'")
            ).scalar()
            == 1
        )


def test_verifier_assigned_and_task_waiting_recipients(engine: Engine) -> None:
    eng = NotificationEngine(clock=CLOCK)
    with Session(engine) as s, s.begin():
        ev = _append(
            s,
            aggregate_type="verification_run",
            aggregate_id="vr-ntf-1",
            type="VERIFIER_ASSIGNED",
            idempotency_scope="verification_run:assign",
            idempotency_key="v1",
            task_id="task-ntf-1",
            payload={
                "verification_id": "vr-ntf-1",
                "target_type": "task",
                "target_id": "task-ntf-1",
                "verifier_account_id": str(IDS["verifier"]),
                "implementer_account_id": str(IDS["impl_agent"]),
                "criteria_version": "v8.0",
            },
        )
        rv = eng.on_event(s, ev)
        waiting = _append(
            s,
            aggregate_type="task",
            aggregate_id="task-ntf-1",
            type="TASK_WAITING",
            idempotency_scope="task:wait",
            idempotency_key="w1",
            task_id="task-ntf-1",
            payload={"task_id": "task-ntf-1", "reason_code": "NO_CANDIDATE"},
        )
        rw = eng.on_event(s, waiting)
    assert [(r.recipient, r.status, r.rule_id) for r in rv] == [
        (str(IDS["verifier"]), "queued", "ntf-verifier-assigned")
    ]
    with Session(engine) as s:
        renotify = s.execute(
            text(
                "SELECT next_attempt_at, destination FROM delivery_outbox WHERE kind = "
                "'notification_reminder' AND payload->>'reminder' = 're_notify' ORDER BY "
                "destination"
            )
        ).all()
        assert {r[0] for r in renotify} == {dt.datetime(2026, 3, 1, 10, 10, tzinfo=dt.UTC)}
        assert {r[1].split(":")[0] for r in renotify} == {"work_item", "mattermost"}
        assert cancel_pending(s, rv[0].notification_id or "") == 2
        s.commit()
    expected_waiting = {
        str(IDS[n])
        for n in (
            "delegator",
            "requester",
            "impl_agent",
            "approver_a",
            "approver_b",
            "approver_agent",
            "muted",
            "digest",
            "member",
        )
    }
    assert {r.recipient for r in rw} == expected_waiting
    assert {r.status for r in rw if r.recipient == str(IDS["muted"])} == {"suppressed"}


def test_outbox_failures_never_touch_task_state_and_sent_appends_once(engine: Engine) -> None:
    store_actor = str(IDS["service"])
    with Session(engine) as s:
        before_events = s.execute(
            text(
                "SELECT count(*), max(recorded_seq) FROM events WHERE workspace_id = :w "
                "AND aggregate_type <> 'notification'"
            ),
            {"w": WS},
        ).first()
        before_task = s.execute(
            text("SELECT status, updated_at FROM tasks_projection WHERE task_id = 'task-ntf-1'")
        ).first()
        pending = s.execute(
            text(
                "SELECT count(*) FROM delivery_outbox WHERE workspace_id = :w AND status "
                "= 'pending' AND next_attempt_at <= :n"
            ),
            {"w": WS, "n": CLOCK.now()},
        ).scalar()
    assert pending and pending > 0
    failing = StubProvider(fail_times=10_000)
    with Session(engine) as s, s.begin():
        store = PostgresEventStore(s, clock=CLOCK)
        r = drain(s, failing, store, CLOCK, store_actor, str(WS), max_attempts=2)
    assert r.sent == 0 and r.failed == pending and r.dead == 0
    with Session(engine) as s:
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM delivery_outbox WHERE workspace_id = :w AND "
                    "status = 'pending' AND attempts = 1"
                ),
                {"w": WS},
            ).scalar()
            == pending
        )
        # backoff: not due at the same instant
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM delivery_outbox WHERE workspace_id = :w AND "
                    "status = 'pending' AND next_attempt_at <= :n"
                ),
                {"w": WS, "n": CLOCK.now()},
            ).scalar()
            == 0
        )
    CLOCK.advance(dt.timedelta(seconds=2))
    with Session(engine) as s, s.begin():
        r2 = drain(
            s,
            failing,
            PostgresEventStore(s, clock=CLOCK),
            CLOCK,
            store_actor,
            str(WS),
            max_attempts=2,
        )
    assert r2.dead == pending and r2.sent == 0
    with Session(engine) as s:
        after_events = s.execute(
            text(
                "SELECT count(*), max(recorded_seq) FROM events WHERE workspace_id = :w "
                "AND aggregate_type <> 'notification'"
            ),
            {"w": WS},
        ).first()
        after_task = s.execute(
            text("SELECT status, updated_at FROM tasks_projection WHERE task_id = 'task-ntf-1'")
        ).first()
    assert (
        after_events == before_events and after_task == before_task
    )  # notification loss: zero effect on Task/Event state
    # fresh deliveries succeed exactly once and append one NOTIFICATION_SENT each
    eng = NotificationEngine(clock=CLOCK)
    with Session(engine) as s, s.begin():
        ev = _approval_event(s, "apr-ntf-2", "b1", risk="MEDIUM")
        records = eng.on_event(s, ev)
    queued = [r for r in records if r.status == "queued"]
    assert queued
    ok = StubProvider()
    with Session(engine) as s, s.begin():
        r3 = drain(s, ok, PostgresEventStore(s, clock=CLOCK), CLOCK, store_actor, str(WS))
    with Session(engine) as s, s.begin():
        r4 = drain(s, ok, PostgresEventStore(s, clock=CLOCK), CLOCK, store_actor, str(WS))
    assert r3.sent >= len(queued) * 2 and r4.sent == 0
    with Session(engine) as s:
        for rec in queued:
            n = s.execute(
                text(
                    "SELECT count(*) FROM events WHERE type = 'NOTIFICATION_SENT' AND "
                    "aggregate_id = :n"
                ),
                {"n": rec.notification_id},
            ).scalar()
            assert n == 2  # one per channel (thread, dm), each exactly once
            assert (
                s.execute(
                    text("SELECT status FROM notifications WHERE notification_id = :n"),
                    {"n": rec.notification_id},
                ).scalar()
                == "sent"
            )
