"""P2-05/P2-06 Bridge behaviour on the real schema with fake providers:
V-P2-03 (per-channel pairs), V-P2-04 (100+100 messages, zero loss/duplicates/echo), V-P2-05
(direction policy), V-P2-06 (thread mapping round trips), V-P2-07 (loop markers), V-P2-08
(10-minute outage → exactly-once after recovery), V-P2-10 (canary redaction), V-P2-13 (disable
only A), V-P2-14 (mapping integrity), V-P2-15 (latency p95 ≤ 5 s at 10 msg/s), V-P2-17
(duplicate Telegram target)."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator
from typing import Any, cast

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application import bridges as bridge_cmds
from server.application import bus
from server.channels import telegram_contract as tc
from server.channels.outbox import RecordingChannelProvider
from server.channels.telegram.bridge import (
    ORIGIN_PROP,
    Bridge,
    MattermostPostView,
    TelegramBridgeProvider,
)
from server.channels.telegram.client import FakeTelegramClient
from server.channels.telegram.intake import InboundMessage
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore
from server.policy.repository import PostgresPolicyRepository

pytestmark = pytest.mark.db

WS = uuid.uuid4()
ADMIN = uuid.uuid4()
MEMBER = uuid.uuid4()
SERVICE = uuid.uuid4()
MM_PI = uuid.uuid4()
CHAN_A = uuid.uuid4()
CHAN_B = uuid.uuid4()
CHAN_C = uuid.uuid4()
TG_PI = "tg:424242"
CHAT_A = "-1001000000001"
CHAT_B = "-1001000000002"
T0 = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
CANARY = "CANARY-NOT-A-SECRET-7777"


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-br', 'br')"),
            {"i": WS},
        )
        for acc, name, typ in (
            (ADMIN, "acct-br-admin", "human"),
            (MEMBER, "acct-br-member", "human"),
            (SERVICE, "acct-br-svc", "service"),
        ):
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) "
                    "VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc, "a": name, "w": WS, "t": typ},
            )
        s.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, provider, "
                "base_url, team_or_bot_ref, bot_user_id) VALUES (:i, 'mm:test:colab', :w, "
                "'mattermost', "
                "'http://mm', 'colab', 'mmbot')"
            ),
            {"i": MM_PI, "w": WS},
        )
        for cid, pub, ext in (
            (CHAN_A, "chan-br-a", "ext-a"),
            (CHAN_B, "chan-br-b", "ext-b"),
            (CHAN_C, "chan-br-c", "ext-c"),
        ):
            s.execute(
                text(
                    "INSERT INTO channels (id, channel_id, workspace_id, provider_instance_id, "
                    "external_channel_id, channel_type, display_name) VALUES (:i, :p, :w, :pi, :e, "
                    "'work', :p)"
                ),
                {"i": cid, "p": pub, "w": WS, "pi": MM_PI, "e": ext},
            )
            for acc in (ADMIN, MEMBER):
                s.execute(
                    text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                    {"c": cid, "a": acc},
                )
        repo = PostgresPolicyRepository()
        repo.create_role(s, WS, "br-admin", "admin")
        repo.commit_role_version(
            s, "br-admin", ["bridge.manage", "admin.settings", "channel.manage"], [], {}, ADMIN
        )
        repo.assign_role(s, ADMIN, "br-admin", ADMIN, T0)
        repo.create_role(s, "br-member", "member") if False else None
        repo.create_role(s, WS, "br-member", "member")
        repo.commit_role_version(s, "br-member", ["task.read"], [], {}, ADMIN)
        repo.assign_role(s, MEMBER, "br-member", ADMIN, T0)
    yield eng
    eng.dispose()


def _ctx(
    s: Session, who: uuid.UUID, key: str, clock: FixedClock, **extras: Any
) -> bus.CommandContext:
    account_id = {ADMIN: "acct-br-admin", MEMBER: "acct-br-member", SERVICE: "acct-br-svc"}[who]
    from server.application.authz import BusAuthorizer

    return bus.CommandContext(
        session=s,
        store=PostgresEventStore(s, clock=clock),
        authorizer=BusAuthorizer(),
        clock=clock,
        principal=bus.Principal(account_id, str(who), "human", f"fp-{account_id}"),
        workspace_id=str(WS),
        correlation_id=f"corr-{key}",
        idempotency_key=key,
        extras=extras,
    )


def _create(s: Session, clock: FixedClock, key: str, channel: str, chat: str, **over: Any) -> str:
    cmd = bridge_cmds.CreateBridge(
        channel_id=channel, provider_instance_id=TG_PI, telegram_chat_id=chat, **over
    )
    return bus.execute(cmd, _ctx(s, ADMIN, key, clock)).resource_id


def _mm_post(
    channel_ext: str, post_id: str, message: str, root_id: str | None = None, **over: Any
) -> MattermostPostView:
    base: dict[str, Any] = {
        "provider_instance_id": "mm:test:colab",
        "channel_ext_id": channel_ext,
        "post_id": post_id,
        "root_id": root_id,
        "user_id": "u-human",
        "user_label": "alice",
        "message": message,
    }
    base.update(over)
    return MattermostPostView(**base)


def _tg_msg(
    chat: str,
    message_id: int,
    text_body: str,
    thread: int | None = None,
    reply: int | None = None,
    **over: Any,
) -> InboundMessage:
    base: dict[str, Any] = {
        "provider_instance_id": TG_PI,
        "update_id": message_id,
        "chat_id": chat,
        "message_id": message_id,
        "date": 1_780_000_000,
        "message_thread_id": thread,
        "reply_to_message_id": reply,
        "from_user_id": "777",
        "from_is_bot": False,
        "from_display_name": "tguser",
        "text": text_body,
        "is_topic_message": thread is not None,
    }
    base.update(over)
    return InboundMessage(**base)


@pytest.fixture(scope="module")
def bridges(engine: Engine) -> dict[str, str]:
    """Two bridges: channel A ↔ chat A, channel B ↔ chat B (bidirectional, topic per root)."""
    clock = FixedClock(T0)
    with Session(engine) as s, s.begin():
        a = _create(s, clock, "br-create-a", "chan-br-a", CHAT_A, bridge_id="bridge-a1")
        b = _create(s, clock, "br-create-b", "chan-br-b", CHAT_B, bridge_id="bridge-b1")
    return {"a": a, "b": b}


def _fake(tg: TelegramBridgeProvider) -> FakeTelegramClient:
    return cast(FakeTelegramClient, tg.client)


def _providers() -> tuple[TelegramBridgeProvider, RecordingChannelProvider]:
    return TelegramBridgeProvider(FakeTelegramClient()), RecordingChannelProvider(
        prefix="mattermost"
    )


def test_per_channel_isolation_direction_mapping_and_disable(
    engine: Engine, bridges: dict[str, str]
) -> None:
    """V-P2-03, V-P2-05, V-P2-06, V-P2-13, V-P2-14."""
    clock = FixedClock(T0)
    bridge = Bridge(mm_bot_user_ids={"mmbot"})
    tg, mm = _providers()
    with Session(engine) as s, s.begin():
        # MM root in channel A → Telegram chat A only
        out = bridge.on_mattermost_post(s, clock, _mm_post("ext-a", "post-a1", "hello from A"))
        assert [o.code for o in out] == ["ENQUEUED"]
        bridge.deliver(s, {"telegram": tg, "mattermost": mm}, clock, str(WS))
        rows = s.execute(
            text(
                "SELECT bridge_id, delivery_status, tg_chat_id, tg_message_id, tg_thread_id FROM "
                "message_mappings WHERE source_message_id = 'post-a1'"
            )
        ).all()
        assert (
            len(rows) == 1
            and rows[0][0] == "bridge-a1"
            and rows[0][1] == "sent"
            and rows[0][2] == CHAT_A
        )
        assert rows[0][3] is not None and rows[0][4] is not None  # topic created for the root
        topic = int(rows[0][4])
        sends = [c for c in _fake(tg).calls if c[0] == "sendMessage"]
        assert (
            len(sends) == 1
            and sends[0][1]["chat_id"] == CHAT_A
            and "[alice via Mattermost]" in sends[0][1]["text"]
        )
        # MM reply in the thread → reply inside the topic with reply_parameters
        out = bridge.on_mattermost_post(
            s, clock, _mm_post("ext-a", "post-a2", "reply", root_id="post-a1")
        )
        assert out[0].accepted and out[0].target is not None and out[0].target.tg_thread_id == topic
        bridge.deliver(s, {"telegram": tg, "mattermost": mm}, clock, str(WS))
        last = [c for c in _fake(tg).calls if c[0] == "sendMessage"][-1][1]
        assert last["message_thread_id"] == topic and last["reply_to_message_id"] == int(rows[0][3])
        # Telegram message in the mapped topic of chat A → MM thread reply under post-a1 (V-P2-06)
        out = bridge.on_telegram_message(
            s, clock, _tg_msg(CHAT_A, 5001, "from telegram", thread=topic)
        )
        assert (
            [o.code for o in out] == ["ENQUEUED"]
            and out[0].target is not None
            and out[0].target.mm_root_id == "post-a1"
        )
        bridge.deliver(s, {"telegram": tg, "mattermost": mm}, clock, str(WS))
        posted = mm.calls[-1][1]
        assert (
            mm.calls[-1][0] == "mattermost:ext-a"
            and posted["root_id"] == "post-a1"
            and posted["message"].startswith("[tguser via Telegram]")
        )
        assert posted["props"][ORIGIN_PROP]["origin"] == "telegram:5001"
        # chat B message never reaches channel A (V-P2-03)
        out = bridge.on_telegram_message(s, clock, _tg_msg(CHAT_B, 6001, "b only"))
        assert [o.bridge_id for o in out] == ["bridge-b1"]
        bridge.deliver(s, {"telegram": tg, "mattermost": mm}, clock, str(WS))
        assert mm.calls[-1][0] == "mattermost:ext-b"
        assert all(c[0] != "mattermost:ext-a" or "b only" not in c[1]["message"] for c in mm.calls)
    # direction policy (V-P2-05): make bridge B one-way MM→TG, then a Telegram message is denied
    with Session(engine) as s, s.begin():
        bus.execute(
            bridge_cmds.UpdateBridge("bridge-b1", {"direction": "mattermost_to_telegram"}),
            _ctx(s, ADMIN, "br-upd-b", clock),
        )
        before = s.execute(text("SELECT count(*) FROM delivery_outbox")).scalar_one()
        out = bridge.on_telegram_message(s, clock, _tg_msg(CHAT_B, 6002, "reverse"))
        assert [o.code for o in out] == ["BRIDGE_DIRECTION_DENIED"]
        assert s.execute(text("SELECT count(*) FROM delivery_outbox")).scalar_one() == before
        audit = s.execute(
            text(
                "SELECT count(*) FROM audit_events WHERE action = 'bridge.direction_denied' AND "
                "target_id = 'bridge-b1'"
            )
        ).scalar_one()
        assert audit == 1 and bridge.metrics.direction_denied == 1
        bus.execute(
            bridge_cmds.UpdateBridge("bridge-b1", {"direction": "bidirectional"}),
            _ctx(s, ADMIN, "br-upd-b2", clock),
        )
    # disable only A (V-P2-13)
    with Session(engine) as s, s.begin():
        r = bus.execute(bridge_cmds.DisableBridge("bridge-a1"), _ctx(s, ADMIN, "br-dis-a", clock))
        assert r.data["status"] == "disabled"
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM events WHERE type = 'TELEGRAM_BRIDGE_DISABLED' AND "
                    "aggregate_id = 'bridge-a1'"
                )
            ).scalar_one()
            == 1
        )
        out_a = bridge.on_mattermost_post(s, clock, _mm_post("ext-a", "post-a3", "while disabled"))
        out_b = bridge.on_mattermost_post(s, clock, _mm_post("ext-b", "post-b3", "b still works"))
        assert [o.code for o in out_a] == ["BRIDGE_DISABLED"] and [o.code for o in out_b] == [
            "ENQUEUED"
        ]
        bus.execute(bridge_cmds.EnableBridge("bridge-a1"), _ctx(s, ADMIN, "br-en-a", clock))
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM events WHERE type = 'TELEGRAM_BRIDGE_ENABLED' AND "
                    "aggregate_id = 'bridge-a1'"
                )
            ).scalar_one()
            == 2
        )
        bridge.deliver(s, {"telegram": tg, "mattermost": mm}, clock, str(WS))
    # mapping integrity (V-P2-14)
    with Session(engine) as s:
        rows = s.execute(
            text(
                "SELECT source_platform, source_message_id, destination_message_id, origin_marker, "
                "delivery_status, dedupe_key FROM message_mappings"
            )
        ).all()
        assert rows and len({(r[0], r[1]) for r in rows}) == len(rows)
        assert all(r[3].startswith("colab-bridge:") for r in rows)
        assert all(r[2] is not None for r in rows if r[4] == "sent")
        assert len({r[5] for r in rows}) == len(rows)
        assert all(
            r[5]
            == tc.mapping_key(
                "bridge-a1" if r[1].startswith(("post-a", "5")) else "bridge-b1", r[0], r[1]
            )
            for r in rows
        )


def test_hundred_messages_each_way_zero_loss_duplicates_echo(
    engine: Engine, bridges: dict[str, str]
) -> None:
    """V-P2-04 and V-P2-07 (re-injected/altered markers blocked)."""
    clock = FixedClock(T0 + dt.timedelta(hours=1))
    bridge = Bridge(mm_bot_user_ids={"mmbot"})
    tg, mm = _providers()
    providers: dict[str, Any] = {"telegram": tg, "mattermost": mm}
    with Session(engine) as s, s.begin():
        for i in range(100):
            assert bridge.on_mattermost_post(
                s, clock, _mm_post("ext-a", f"mm-{i}", f"mm message {i}")
            )[0].accepted
            assert bridge.on_telegram_message(
                s, clock, _tg_msg(CHAT_A, 10_000 + i, f"tg message {i}")
            )[0].accepted
            if i % 10 == 9:
                bridge.deliver(s, providers, clock, str(WS))
        bridge.deliver(s, providers, clock, str(WS))
        tg_sends = [c for c in _fake(tg).calls if c[0] == "sendMessage"]
        mm_posts = [c for c in mm.calls if c[0] == "mattermost:ext-a"]
        assert len(tg_sends) == 100 and len(mm_posts) == 100
        assert (
            len({c[1]["text"] for c in tg_sends}) == 100
            and len({c[1]["message"] for c in mm_posts}) == 100
        )
        # echo: re-inject every delivered message as if observed again on its destination platform
        blocked = 0
        for c in mm_posts:  # posts created by our bot in Mattermost
            view = _mm_post(
                "ext-a",
                f"echo-{blocked}",
                c[1]["message"],
                user_id="mmbot",
                user_is_bot=True,
                props=c[1]["props"],
            )
            assert [o.code for o in bridge.on_mattermost_post(s, clock, view)] == [
                "BRIDGE_LOOP_DETECTED"
            ]
            blocked += 1
        for n, c in enumerate(
            tg_sends
        ):  # messages our bot sent to Telegram, observed via getUpdates
            msg = _tg_msg(CHAT_A, 20_000 + n, c[1]["text"], from_user_id="424242", from_is_bot=True)
            assert [o.code for o in bridge.on_telegram_message(s, clock, msg)] == [
                "BRIDGE_LOOP_DETECTED"
            ]
            blocked += 1
        # altered markers: a human re-posting a relayed text keeps the prefix → still a loop; a
        # forged prop with hop 1 → blocked by the hop limit; a replayed source id → duplicate
        forged = _mm_post(
            "ext-a", "forged-1", "innocent", props={ORIGIN_PROP: {"origin": "telegram:1", "hop": 1}}
        )
        assert [o.code for o in bridge.on_mattermost_post(s, clock, forged)] == [
            "BRIDGE_LOOP_DETECTED"
        ]
        copied = _mm_post("ext-a", "copied-1", tg_sends[0][1]["text"])
        assert [o.code for o in bridge.on_mattermost_post(s, clock, copied)] == [
            "BRIDGE_LOOP_DETECTED"
        ]
        assert [
            o.code
            for o in bridge.on_mattermost_post(s, clock, _mm_post("ext-a", "mm-0", "mm message 0"))
        ] == ["BRIDGE_DUPLICATE_SOURCE"]
        assert [
            o.code
            for o in bridge.on_telegram_message(s, clock, _tg_msg(CHAT_A, 10_000, "tg message 0"))
        ] == ["BRIDGE_DUPLICATE_SOURCE"]
        bridge.deliver(s, providers, clock, str(WS))
        assert (
            len([c for c in _fake(tg).calls if c[0] == "sendMessage"]) == 100
            and len([c for c in mm.calls if c[0] == "mattermost:ext-a"]) == 100
        )
        assert (
            bridge.metrics.loops_blocked == blocked + 2 and bridge.metrics.duplicates_prevented == 2
        )
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM message_mappings WHERE bridge_id = 'bridge-a1' AND "
                    "delivery_status = 'sent'"
                )
            ).scalar_one()
            >= 200
        )


def test_outage_recovery_exactly_once_and_dead_letters(
    engine: Engine, bridges: dict[str, str]
) -> None:
    """V-P2-08: 10-minute provider outage → core continues, exactly-once after recovery."""
    clock = FixedClock(T0 + dt.timedelta(hours=2))
    bridge = Bridge(store=None)
    tg = TelegramBridgeProvider(FakeTelegramClient(fail_forbidden_chats={CHAT_B}))
    mm = RecordingChannelProvider(prefix="mattermost")
    providers: dict[str, Any] = {"telegram": tg, "mattermost": mm}
    with Session(engine) as s, s.begin():
        for i in range(5):
            assert bridge.on_mattermost_post(
                s, clock, _mm_post("ext-b", f"outage-{i}", f"during outage {i}")
            )[0].accepted
        # the outage lasts 10 minutes: every drain fails, the core keeps accepting messages
        for _ in range(20):
            bridge.deliver(s, providers, clock, str(WS))
            clock.advance(dt.timedelta(seconds=30))
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM message_mappings WHERE bridge_id = 'bridge-b1' AND "
                    "delivery_status = 'sent' AND source_message_id LIKE 'outage-%'"
                )
            ).scalar_one()
            == 0
        )
        assert (
            s.execute(
                text("SELECT count(*) FROM delivery_outbox WHERE status = 'dead'")
            ).scalar_one()
            == 0
        )
        # recovery
        _fake(tg).fail_forbidden_chats.clear()
        for _ in range(30):
            bridge.deliver(s, providers, clock, str(WS))
            clock.advance(dt.timedelta(seconds=30))
        sends = [
            c
            for c in _fake(tg).calls
            if c[0] == "sendMessage"
            and c[1]["chat_id"] == CHAT_B
            and "during outage" in c[1]["text"]
        ]
        assert len(sends) == 5  # exactly once each
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM message_mappings WHERE bridge_id = 'bridge-b1' AND "
                    "delivery_status = 'sent' AND source_message_id LIKE 'outage-%'"
                )
            ).scalar_one()
            == 5
        )
    # permanent failure → dead letter after the maximum attempts, replayable exactly once
    clock2 = FixedClock(T0 + dt.timedelta(hours=5))
    dead_tg = TelegramBridgeProvider(FakeTelegramClient(fail_forbidden_chats={CHAT_B}))
    with Session(engine) as s, s.begin():
        assert bridge.on_mattermost_post(s, clock2, _mm_post("ext-b", "perm-1", "permanent"))[
            0
        ].accepted
        for _ in range(12):
            bridge.deliver(
                s, {"telegram": dead_tg, "mattermost": mm}, clock2, str(WS), max_attempts=3
            )
            clock2.advance(dt.timedelta(seconds=60))
        assert (
            s.execute(
                text("SELECT count(*) FROM bridge_dead_letters WHERE bridge_id = 'bridge-b1'")
            ).scalar_one()
            == 1
        )
        assert (
            s.execute(
                text(
                    "SELECT delivery_status FROM message_mappings WHERE source_message_id = "
                    "'perm-1'"
                )
            ).scalar_one()
            == "dead"
        )
        _fake(dead_tg).fail_forbidden_chats.clear()
        assert bridge.replay_dead_letters(s, clock2, str(WS)) == 1
        assert bridge.replay_dead_letters(s, clock2, str(WS)) == 0  # exactly once
        bridge.deliver(s, {"telegram": dead_tg, "mattermost": mm}, clock2, str(WS))
        assert (
            s.execute(
                text(
                    "SELECT delivery_status FROM message_mappings WHERE source_message_id = "
                    "'perm-1'"
                )
            ).scalar_one()
            == "sent"
        )
        assert len([c for c in _fake(dead_tg).calls if c[0] == "sendMessage"]) == 1


def test_canary_never_leaves_the_raw_fixture(engine: Engine, bridges: dict[str, str]) -> None:
    """V-P2-10: the raw canary exists only in this test; persisted/forwarded copies are redacted."""
    clock = FixedClock(T0 + dt.timedelta(hours=3))
    bridge = Bridge()
    tg, mm = _providers()
    with Session(engine) as s, s.begin():
        out = bridge.on_mattermost_post(
            s, clock, _mm_post("ext-a", "canary-1", f"token {CANARY} and password=Hunter2Secret!")
        )
        assert out[0].accepted
        out2 = bridge.on_telegram_message(s, clock, _tg_msg(CHAT_A, 30_001, f"see {CANARY}"))
        assert out2[0].accepted
        bridge.deliver(s, {"telegram": tg, "mattermost": mm}, clock, str(WS))
        for table, column in (
            ("message_mappings", "redaction_status"),
            ("delivery_outbox", "payload::text"),
            ("events", "payload::text"),
            ("audit_events", "redacted_metadata::text"),
        ):
            hits = s.execute(
                text(f"SELECT count(*) FROM {table} WHERE {column} LIKE :c"),  # noqa: S608
                {"c": f"%{CANARY}%"},
            ).scalar_one()
            assert hits == 0, table
        assert all(CANARY not in json.dumps(c[1]) for c in _fake(tg).calls) and all(
            CANARY not in json.dumps(c[1]) for c in mm.calls
        )
        status = s.execute(
            text(
                "SELECT redaction_status FROM message_mappings WHERE source_message_id = 'canary-1'"
            )
        ).scalar_one()
        assert status.startswith("redacted:") and "canary" in status
        assert (
            s.execute(
                text("SELECT count(*) FROM audit_events WHERE action = 'bridge.redacted'")
            ).scalar_one()
            >= 2
        )


def test_delivery_latency_p95_under_ten_messages_per_second(
    engine: Engine, bridges: dict[str, str]
) -> None:
    """V-P2-15: 100 deliveries at 10 msg/s (virtual clock, drain every second) → p95 ≤ 5 s."""
    clock = FixedClock(T0 + dt.timedelta(hours=4))
    bridge = Bridge()
    tg, mm = _providers()
    providers: dict[str, Any] = {"telegram": tg, "mattermost": mm}
    with Session(engine) as s, s.begin():
        for i in range(100):
            assert bridge.on_mattermost_post(
                s, clock, _mm_post("ext-b", f"lat-{i}", f"latency {i}")
            )[0].accepted
            clock.advance(dt.timedelta(milliseconds=100))
            if i % 10 == 9:
                bridge.deliver(s, providers, clock, str(WS))
        bridge.deliver(s, providers, clock, str(WS))
        rows = s.execute(
            text(
                "SELECT EXTRACT(EPOCH FROM (delivered_at - created_at)) * 1000 FROM "
                "message_mappings WHERE bridge_id = 'bridge-b1' AND source_message_id LIKE 'lat-%' "
                "AND delivery_status = 'sent'"
            )
        ).all()
        latencies = sorted(int(r[0]) for r in rows)
        assert len(latencies) == 100
        p95 = latencies[int(len(latencies) * 0.95) - 1]
        print(f"V-P2-15 latency_ms: p95={p95} max={max(latencies)} n={len(latencies)}")
        assert p95 <= 5_000 and max(latencies) <= 15_000, (p95, max(latencies))
        assert (
            len([c for c in _fake(tg).calls if c[0] == "sendMessage" and "latency" in c[1]["text"]])
            == 100
        )


def test_duplicate_telegram_target_rejected_unless_admin_exception(
    engine: Engine, bridges: dict[str, str]
) -> None:
    """V-P2-17 and the unauthorized half of V-P2-12 at the API/bus level."""
    clock = FixedClock(T0 + dt.timedelta(hours=6))
    with Session(engine) as s, s.begin():
        with pytest.raises(bus.CommandError) as exc:
            _create(s, clock, "br-dup-1", "chan-br-c", CHAT_A)
        assert exc.value.code == "BRIDGE_TARGET_DUPLICATE"
    with Session(engine) as s, s.begin():
        with pytest.raises(bus.CommandError) as exc2:  # a member without bridge.manage
            bus.execute(
                bridge_cmds.CreateBridge(
                    channel_id="chan-br-c", provider_instance_id=TG_PI, telegram_chat_id=CHAT_B
                ),
                _ctx(s, MEMBER, "br-dup-2", clock),
            )
        assert exc2.value.status in (403, 404)
    with Session(engine) as s, s.begin():
        bid = _create(
            s,
            clock,
            "br-dup-3",
            "chan-br-c",
            CHAT_A,
            admin_exception=True,
            admin_exception_reason="ops mirror",
            bridge_id="bridge-c1",
        )
        row = s.execute(
            text(
                "SELECT admin_exception, admin_exception_reason FROM telegram_bridges WHERE "
                "bridge_id = :b"
            ),
            {"b": bid},
        ).first()
        assert row is not None and row[0] is True and row[1] == "ops mirror"
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE action = 'bridge.admin_exception' AND "
                    "target_id = :b"
                ),
                {"b": bid},
            ).scalar_one()
            == 1
        )
        status = bridge_cmds.bridge_status(_ctx(s, ADMIN, "br-status", clock), "bridge-a1")
        assert status["bridge_id"] == "bridge-a1" and "deliveries" in status
