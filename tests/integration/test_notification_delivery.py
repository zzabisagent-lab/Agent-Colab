"""V-P2-31: approval/verifier/waiting notifications reach mention/DM/approval channel; digest
batched hourly; zero when muted; Telegram relay follows the Bridge policy."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.channels.mattermost.client import FakeMattermostClient
from server.channels.mattermost.provider import ProviderInstance
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore
from server.events.store import AppendRequest
from server.notifications.outbox import drain
from server.notifications.providers import (
    CompositeProvider,
    MattermostNotificationProvider,
    NoopProvider,
    TelegramRelayGate,
)
from server.notifications.routing import DigestScheduler, get_preferences, set_preferences
from server.notifications.rules import NotificationEngine, load_rules, sync_rules

pytestmark = pytest.mark.db

WS = uuid.uuid4()
PI = uuid.uuid4()
CHANNEL = uuid.uuid4()
APPROVAL_CHANNEL = uuid.uuid4()
IDS = {
    n: uuid.uuid4()
    for n in (
        "requester",
        "impl_agent",
        "approver_a",
        "approver_b",
        "muted",
        "digest",
        "verifier",
        "delegator",
        "admin",
        "service",
    )
}
MM_USER = {n: f"mmuser-{n}" for n in IDS}
CLOCK = FixedClock(dt.datetime(2026, 4, 1, 10, 0, tzinfo=dt.UTC))
EXT_WORK, EXT_APPROVALS, ROOT_POST = "mmchan-ntf-work", "mmchan-ntf-approvals", "root-post-task-1"


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-nd', 'nd')"),
            {"i": WS},
        )
        for name, acc in IDS.items():
            typ = "agent" if name == "impl_agent" else "service" if name == "service" else "human"
            c.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) "
                    "VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc, "a": f"acct-nd-{name}", "w": WS, "t": typ},
            )
        c.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, provider, "
                "base_url, team_or_bot_ref, bot_user_id) "
                "VALUES (:i, 'mm:test:nd', :w, 'mattermost', 'http://mm', 'team-nd', 'bot-user')"
            ),
            {"i": PI, "w": WS},
        )
        for cid, ctype, ext in (
            (CHANNEL, "work", EXT_WORK),
            (APPROVAL_CHANNEL, "approval", EXT_APPROVALS),
        ):
            c.execute(
                text(
                    "INSERT INTO channels (id, channel_id, workspace_id, provider_instance_id, "
                    "external_channel_id, channel_type, display_name) "
                    "VALUES (:i, :c, :w, :p, :e, :t, :t)"
                ),
                {"i": cid, "c": f"chan-nd-{ctype}", "w": WS, "p": PI, "e": ext, "t": ctype},
            )
        for name in (
            "requester",
            "impl_agent",
            "approver_a",
            "approver_b",
            "muted",
            "digest",
            "delegator",
        ):
            c.execute(
                text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                {"c": CHANNEL, "a": IDS[name]},
            )
        # every human is linked to a Mattermost user on the instance
        for name, acc in IDS.items():
            c.execute(
                text(
                    "INSERT INTO external_identity_links (id, link_id, provider_instance_id, "
                    "external_user_id, account_id, verification_method, status, verified_at) "
                    "VALUES (:i, :l, :p, :u, :a, 'signed_challenge', 'active', now())"
                ),
                {"i": uuid.uuid4(), "l": f"link-nd-{name}", "p": PI, "u": MM_USER[name], "a": acc},
            )
        c.execute(
            text(
                "INSERT INTO roles (id, role_id, workspace_id, display_name, current_version) "
                "VALUES (:i, 'role-nd-approver', :w, 'approver', 1)"
            ),
            {"i": uuid.uuid4(), "w": WS},
        )
        c.execute(
            text(
                "INSERT INTO role_versions (id, role_id, version, permissions, deny, constraints, "
                "policy_hash, created_by) VALUES (:i, 'role-nd-approver', 1, CAST(:p AS jsonb), "
                "'[]', '{}', 'h', :by)"
            ),
            {
                "i": uuid.uuid4(),
                "p": json.dumps(["approval.decide", "task.read"]),
                "by": IDS["admin"],
            },
        )
        for name in ("approver_a", "approver_b", "muted", "digest", "requester"):
            c.execute(
                text(
                    "INSERT INTO principal_role_assignments (id, account_id, role_id, assigned_by, "
                    "valid_from) VALUES (:i, :a, 'role-nd-approver', :by, :vf)"
                ),
                {
                    "i": uuid.uuid4(),
                    "a": IDS[name],
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
                "INSERT INTO tasks_projection (task_id, workspace_id, root_task_id, channel_id, "
                "title, "
                "domain, risk, status, delegated_by, created_at, updated_at) VALUES ('task-nd-1', "
                ":w, "
                "'task-nd-1', :c, 't', 'research', 'LOW', 'WAITING', :d, now(), now())"
            ),
            {"w": WS, "c": CHANNEL, "d": IDS["delegator"]},
        )
        c.execute(
            text(
                "INSERT INTO thread_bindings (provider_instance_id, root_post_id, "
                "external_channel_id, "
                "subject_type, subject_id) VALUES (:p, :r, :e, 'task', 'task-nd-1')"
            ),
            {"p": PI, "r": ROOT_POST, "e": EXT_WORK},
        )
    with Session(eng) as s, s.begin():
        sync_rules(s, str(WS), load_rules())
    yield eng
    eng.dispose()


def _client() -> FakeMattermostClient:
    return FakeMattermostClient(users={MM_USER[n]: {"username": f"u-{n}"} for n in IDS})


def _provider(
    engine: Engine, client: FakeMattermostClient, gate: TelegramRelayGate | None = None
) -> CompositeProvider:
    def resolver(instance: ProviderInstance) -> FakeMattermostClient:
        assert instance.provider_instance_id == "mm:test:nd"
        return client

    mm = MattermostNotificationProvider(
        make_session_factory(engine), resolver, relay_gate=gate, clock=CLOCK
    )
    return CompositeProvider({"mattermost": mm, "work_item": NoopProvider()})


def _append(s: Session, **kw: object) -> dict[str, object]:
    store = PostgresEventStore(s, clock=CLOCK)
    base: dict[str, object] = {
        "workspace_id": str(WS),
        "actor_account_id": str(IDS["requester"]),
        "correlation_id": "corr-nd",
        "channel_id": str(CHANNEL),
    }
    base.update(kw)
    res = store.append(AppendRequest(**base))  # type: ignore[arg-type]
    ev = store.get(res.event_id)
    assert ev is not None
    return ev


def _approval_event(s: Session, approval_id: str, key: str) -> dict[str, object]:
    s.execute(
        text(
            "INSERT INTO approval_grants (id, approval_id, workspace_id, subject_type, subject_id, "
            "action, "
            "risk, status, requested_by, implementing_agent_account_id, channel_id, valid_from, "
            "expires_at) "
            "VALUES (:i, :a, :w, 'task', 'task-nd-1', 'external_send', 'HIGH', 'PENDING', :req, "
            ":impl, :c, "
            ":vf, :ex) ON CONFLICT (approval_id) DO NOTHING"
        ),
        {
            "i": uuid.uuid4(),
            "a": approval_id,
            "w": WS,
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
        task_id="task-nd-1",
        payload={
            "approval_id": approval_id,
            "subject_type": "task",
            "subject_id": "task-nd-1",
            "action": "external_send",
            "risk": "HIGH",
            "expires_at": "2026-04-02T10:00:00.000Z",
        },
    )


def _drain(engine: Engine, provider: CompositeProvider) -> None:
    with Session(engine) as s, s.begin():
        drain(s, provider, PostgresEventStore(s, clock=CLOCK), CLOCK, str(IDS["service"]), str(WS))


def test_approval_verifier_waiting_reach_dm_thread_and_channel(engine: Engine) -> None:
    eng = NotificationEngine(clock=CLOCK)
    client = _client()
    provider = _provider(engine, client)
    with Session(engine) as s, s.begin():
        eng.on_event(s, _approval_event(s, "apr-nd-1", "nd-a1"))
        eng.on_event(
            s,
            _append(
                s,
                aggregate_type="verification_run",
                aggregate_id="vr-nd-1",
                type="VERIFIER_ASSIGNED",
                idempotency_scope="verification_run:assign",
                idempotency_key="nd-v1",
                task_id="task-nd-1",
                payload={
                    "verification_id": "vr-nd-1",
                    "target_type": "task",
                    "target_id": "task-nd-1",
                    "verifier_account_id": str(IDS["verifier"]),
                    "implementer_account_id": str(IDS["impl_agent"]),
                    "criteria_version": "v8.0",
                },
            ),
        )
        eng.on_event(
            s,
            _append(
                s,
                aggregate_type="task",
                aggregate_id="task-nd-1",
                type="TASK_WAITING",
                idempotency_scope="task:wait",
                idempotency_key="nd-w1",
                task_id="task-nd-1",
                payload={"task_id": "task-nd-1", "reason_code": "NO_CANDIDATE"},
            ),
        )
    _drain(engine, provider)
    dm_users = {u for u, _ in client.dms}
    # approvers (HIGH: humans, members, not requester/agent), verifier, delegator get DMs;
    # the muted account gets nothing; the digest account waits for its hourly digest
    assert {
        MM_USER["approver_a"],
        MM_USER["approver_b"],
        MM_USER["verifier"],
        MM_USER["delegator"],
    } <= dm_users
    assert MM_USER["muted"] not in dm_users and MM_USER["digest"] not in dm_users
    thread_posts = [p for p in client.posts.values() if p.root_id == ROOT_POST]
    mentioned = {p.message.split(" ")[0] for p in thread_posts}
    assert {"@u-approver_a", "@u-approver_b"} <= mentioned  # approval thread mentions
    assert any(
        p.channel_id == EXT_APPROVALS for p in client.posts.values()
    )  # approval channel post
    assert not any("mmuser-muted" in m for _, m in client.dms)
    with Session(engine) as s:
        dead = s.execute(
            text(
                "SELECT count(*) FROM delivery_outbox WHERE workspace_id = :w AND status = 'dead'"
            ),
            {"w": WS},
        ).scalar_one()
        sent_events = s.execute(
            text(
                "SELECT count(*) FROM events WHERE workspace_id = :w AND type = 'NOTIFICATION_SENT'"
            ),
            {"w": WS},
        ).scalar_one()
    assert dead == 0 and sent_events >= 4
    # a second drain sends nothing more (exactly once)
    before = len(client.dms) + len(client.posts)
    _drain(engine, provider)
    assert len(client.dms) + len(client.posts) == before


def test_digest_batched_hourly_and_mute_toggle(engine: Engine) -> None:
    client = _client()
    provider = _provider(engine, client)
    scheduler = DigestScheduler(CLOCK)
    with Session(engine) as s:
        pending = scheduler.pending_digests(s, str(WS))
    assert pending and all(
        p["deliver_at"] == dt.datetime(2026, 4, 1, 11, 0, tzinfo=dt.UTC) for p in pending
    )
    _drain(engine, provider)
    assert MM_USER["digest"] not in {u for u, _ in client.dms}  # not yet: the hour has not arrived
    CLOCK.advance(dt.timedelta(hours=1))
    with Session(engine) as s, s.begin():
        flush = scheduler.flush_due(
            s, provider, PostgresEventStore(s, clock=CLOCK), str(IDS["service"]), str(WS)
        )
    assert flush.digests_sent == 1
    digest_dms = [m for u, m in client.dms if u == MM_USER["digest"]]
    assert len(digest_dms) == 1 and digest_dms[0].startswith("Notification digest (")
    # mute via preferences: a new approval yields no DM for the muted-now approver_a
    with Session(engine) as s, s.begin():
        set_preferences(s, str(IDS["approver_a"]), muted=True, clock=CLOCK, workspace_id=str(WS))
        assert get_preferences(s, str(IDS["approver_a"])).muted is True
        NotificationEngine(clock=CLOCK).on_event(s, _approval_event(s, "apr-nd-2", "nd-a2"))
    _drain(engine, provider)
    assert not any(u == MM_USER["approver_a"] and "apr-nd-2" in m for u, m in client.dms)
    assert any(u == MM_USER["approver_b"] and "apr-nd-2" in m for u, m in client.dms)


def test_telegram_relay_follows_bridge_policy(engine: Engine) -> None:
    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "INSERT INTO telegram_bridges (id, bridge_id, workspace_id, channel_id, "
                "provider_instance_id, "
                "telegram_chat_id, direction, content_policy, created_by) VALUES (:i, "
                "'br-nd-allow', :w, :c, "
                "'tg:1', '-1001', 'bidirectional', CAST(:p AS jsonb), :by)"
            ),
            {
                "i": uuid.uuid4(),
                "w": WS,
                "c": APPROVAL_CHANNEL,
                "p": json.dumps({"approval_notice": True}),
                "by": IDS["admin"],
            },
        )
    client = _client()
    provider = _provider(engine, client, TelegramRelayGate())
    with Session(engine) as s, s.begin():
        NotificationEngine(clock=CLOCK).on_event(s, _approval_event(s, "apr-nd-3", "nd-a3"))
    _drain(engine, provider)
    with Session(engine) as s:
        relayed = s.execute(
            text(
                "SELECT destination, payload FROM delivery_outbox WHERE kind = 'telegram.send' AND "
                "workspace_id = :w"
            ),
            {"w": WS},
        ).all()
    assert (
        len(relayed) == 1
        and relayed[0][0] == "telegram:-1001"
        and "apr-nd-3" in json.dumps(relayed[0][1])
    )
    # deny approval notices on the bridge: no relay for the next approval
    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "UPDATE telegram_bridges SET content_policy = CAST(:p AS jsonb) WHERE bridge_id = "
                "'br-nd-allow'"
            ),
            {"p": json.dumps({"approval_notice": False})},
        )
        NotificationEngine(clock=CLOCK).on_event(s, _approval_event(s, "apr-nd-4", "nd-a4"))
    _drain(engine, provider)
    with Session(engine) as s:
        n = s.execute(
            text(
                "SELECT count(*) FROM delivery_outbox WHERE kind = 'telegram.send' AND "
                "workspace_id = :w"
            ),
            {"w": WS},
        ).scalar_one()
    assert n == 1
