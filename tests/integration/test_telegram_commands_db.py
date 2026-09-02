"""P2-08 Telegram command gateway on the real schema (V-P2-16, V-P2-20).

V-P2-16: a Task command from Telegram under the default Bridge policy executes nothing (read/reply
only, one notice per user per hour); when a channel allows commands only the §7A.6 verbs run
(``task show|list``, ``approve show``, ``doc show``) plus verbs opened by the Bridge policy, each
executed through the permission mapping of the command bus.
V-P2-20: the same command by verified-active / unlinked / suspended Telegram users — only the
active link executes with its Account's permissions; the others produce zero Task/Event side
effects and receive a stable reply.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator
from typing import Any, cast

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime
from server.application import bridges as bridge_cmds
from server.application import bus
from server.application.authz import BusAuthorizer
from server.channels import policy as tg_policy
from server.channels.mattermost import provider as prov
from server.channels.mattermost.client import FakeMattermostClient
from server.channels.telegram.bridge import Bridge, TelegramBridgeProvider
from server.channels.telegram.client import FakeTelegramClient
from server.channels.telegram.commands import (
    BRIDGE_NOT_FOUND,
    EXTERNAL_IDENTITY_NOT_ACTIVE,
    NOT_A_COMMAND,
    TELEGRAM_USER_NOT_LINKED,
    TelegramCommandGateway,
    TelegramCommandResult,
)
from server.channels.telegram.intake import InboundMessage
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore
from server.policy.repository import PostgresPolicyRepository

pytestmark = pytest.mark.db

WS = uuid.uuid4()
ADMIN = uuid.uuid4()
ACTIVE = uuid.uuid4()
SUSPENDED = uuid.uuid4()
READER = uuid.uuid4()
MM_PI = uuid.uuid4()
TG_PI_UUID = uuid.uuid4()
MM_PI_ID = "mm:tgcmd:colab"
TG_PI = "tg:515000"
CHAT_A = "-1005150000001"  # default policy: read/reply only
CHAT_B = "-1005150000002"  # commands allowed, §7A.6 verbs only
CHAT_C = "-1005150000003"  # commands allowed + policy opens task.create
CHAT_UNBOUND = "-1005150000009"
USER_ACTIVE = "515001"
USER_SUSPENDED = "515002"
USER_UNLINKED = "515003"
USER_READER = "515004"
T0 = dt.datetime(2026, 7, 1, 9, 0, tzinfo=dt.UTC)
CHANNELS = {"chan-tc-a": ("ext-tc-a", CHAT_A), "chan-tc-b": ("ext-tc-b", CHAT_B)}
CHANNELS["chan-tc-c"] = ("ext-tc-c", CHAT_C)
ACCOUNTS: dict[uuid.UUID, tuple[str, str]] = {
    ADMIN: ("acct-tc-admin", "human"),
    ACTIVE: ("acct-tc-active", "human"),
    SUSPENDED: ("acct-tc-suspended", "human"),
    READER: ("acct-tc-reader", "human"),
}
MEMBER_PERMS = [
    "task.create",
    "task.read",
    "task.list",
    "task.cancel",
    "approval.read",
    "document.read",
    "channel.manage",
]


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    prov.set_client_factory(lambda inst: FakeMattermostClient())
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-tc', 'tc')"),
            {"i": WS},
        )
        for acc, (name, typ) in ACCOUNTS.items():
            s.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc, "a": name, "w": WS, "t": typ},
            )
        s.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, provider, "
                "base_url, team_or_bot_ref, bot_user_id, config) VALUES (:i, :p, :w, 'mattermost', "
                "'http://mm', 'team-tc', 'mmbot', CAST(:cfg AS jsonb))"
            ),
            {"i": MM_PI, "p": MM_PI_ID, "w": WS, "cfg": json.dumps({"team_name": "colab"})},
        )
        s.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, provider, "
                "base_url, team_or_bot_ref) VALUES (:i, :p, :w, 'telegram', "
                "'https://api.telegram.org', '515000')"
            ),
            {"i": TG_PI_UUID, "p": TG_PI, "w": WS},
        )
        for pub, (ext, _chat) in CHANNELS.items():
            cid = uuid.uuid4()
            s.execute(
                text(
                    "INSERT INTO channels (id, channel_id, workspace_id, provider_instance_id, "
                    "external_channel_id, channel_type, display_name) VALUES (:i, :p, :w, :pi, :e, "
                    "'work', :p)"
                ),
                {"i": cid, "p": pub, "w": WS, "pi": MM_PI, "e": ext},
            )
            for acc in (ADMIN, ACTIVE, SUSPENDED, READER):
                s.execute(
                    text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                    {"c": cid, "a": acc},
                )
        for acc, user, status in (
            (ACTIVE, USER_ACTIVE, "active"),
            (SUSPENDED, USER_SUSPENDED, "suspended"),
            (READER, USER_READER, "active"),
        ):
            s.execute(
                text(
                    "INSERT INTO external_identity_links (id, link_id, provider_instance_id, "
                    "external_user_id, account_id, verification_method, status, verified_at) "
                    "VALUES (:i, :l, :p, :u, :a, 'signed_challenge', :st, now())"
                ),
                {
                    "i": uuid.uuid4(),
                    "l": f"link-tc-{user}",
                    "p": TG_PI_UUID,
                    "u": user,
                    "a": acc,
                    "st": status,
                },
            )
        repo = PostgresPolicyRepository()
        repo.create_role(s, WS, "tc-admin", "admin")
        repo.commit_role_version(
            s, "tc-admin", ["bridge.manage", "admin.settings", "channel.manage"], [], {}, ADMIN
        )
        repo.assign_role(s, ADMIN, "tc-admin", ADMIN, T0)
        repo.create_role(s, WS, "tc-member", "member")
        repo.commit_role_version(s, "tc-member", MEMBER_PERMS, [], {}, ADMIN)
        repo.assign_role(s, ACTIVE, "tc-member", ADMIN, T0)
        repo.assign_role(s, SUSPENDED, "tc-member", ADMIN, T0)
        repo.create_role(s, WS, "tc-reader", "member")
        repo.commit_role_version(s, "tc-reader", ["task.read", "task.list"], [], {}, ADMIN)
        repo.assign_role(s, READER, "tc-reader", ADMIN, T0)
    clock = FixedClock(T0)
    with Session(eng) as s, s.begin():
        _create_bridge(s, clock, "tc-bridge-a", "chan-tc-a", CHAT_A, bridge_id="bridge-tc-a")
        _create_bridge(
            s,
            clock,
            "tc-bridge-b",
            "chan-tc-b",
            CHAT_B,
            bridge_id="bridge-tc-b",
            allow_commands=True,
        )
        _create_bridge(
            s,
            clock,
            "tc-bridge-c",
            "chan-tc-c",
            CHAT_C,
            bridge_id="bridge-tc-c",
            allow_commands=True,
        )
        # policy-opened write verb (§7A.6 "any write verbs opened by policy")
        s.execute(
            text(
                "UPDATE telegram_bridges SET content_policy = content_policy || CAST(:cp AS jsonb) "
                "WHERE bridge_id = 'bridge-tc-c'"
            ),
            {"cp": json.dumps({"telegram_commands": {"allowed_verbs": ["task.create"]}})},
        )
    yield eng
    prov.set_client_factory(None)
    eng.dispose()


def _create_bridge(
    s: Session, clock: FixedClock, key: str, channel: str, chat: str, **over: Any
) -> str:
    ctx = bus.CommandContext(
        session=s,
        store=PostgresEventStore(s, clock=clock),
        authorizer=BusAuthorizer(),
        clock=clock,
        principal=bus.Principal("acct-tc-admin", str(ADMIN), "human", "fp-admin"),
        workspace_id=str(WS),
        correlation_id=f"corr-{key}",
        idempotency_key=key,
    )
    cmd = bridge_cmds.CreateBridge(
        channel_id=channel, provider_instance_id=TG_PI, telegram_chat_id=chat, **over
    )
    return bus.execute(cmd, ctx).resource_id


@pytest.fixture
def runtime(engine: Engine) -> Runtime:
    return Runtime(make_session_factory(engine), BusAuthorizer(), None, FixedClock(T0))


def _msg(
    chat: str,
    message_id: int,
    text_body: str,
    *,
    user: str = USER_ACTIVE,
    thread: int | None = None,
    is_bot: bool = False,
) -> InboundMessage:
    return InboundMessage(
        provider_instance_id=TG_PI,
        update_id=message_id,
        chat_id=chat,
        message_id=message_id,
        date=1_780_000_000,
        message_thread_id=thread,
        reply_to_message_id=None,
        from_user_id=user,
        from_is_bot=is_bot,
        from_display_name=f"tg-{user}",
        text=text_body,
        is_topic_message=thread is not None,
    )


def _counts(engine: Engine) -> tuple[int, int, int]:
    """(events, tasks, telegram outbox rows) for the test Workspace."""
    with Session(engine) as s:
        events = s.execute(
            text("SELECT count(*) FROM events WHERE workspace_id = :w"), {"w": WS}
        ).scalar_one()
        tasks = s.execute(
            text("SELECT count(*) FROM tasks_projection WHERE workspace_id = :w"), {"w": WS}
        ).scalar_one()
        outbox = s.execute(
            text(
                "SELECT count(*) FROM delivery_outbox WHERE workspace_id = :w "
                "AND kind = 'telegram.send'"
            ),
            {"w": WS},
        ).scalar_one()
    return int(events), int(tasks), int(outbox)


def _handle(
    engine: Engine, runtime: Runtime, msg: InboundMessage, clock: FixedClock | None = None
) -> TelegramCommandResult:
    gateway = TelegramCommandGateway(runtime, clock or FixedClock(T0))
    with Session(engine) as s, s.begin():
        return gateway.handle(s, msg)


def _deliver(engine: Engine, clock: FixedClock) -> FakeTelegramClient:
    tg = TelegramBridgeProvider(FakeTelegramClient())
    with Session(engine) as s, s.begin():
        Bridge().deliver(s, {"telegram": tg}, clock, str(WS))
    return cast(FakeTelegramClient, tg.client)


def _outbox_payloads(engine: Engine, destination: str) -> list[dict[str, Any]]:
    with Session(engine) as s:
        rows = s.execute(
            text(
                "SELECT payload FROM delivery_outbox WHERE workspace_id = :w AND destination = :d "
                "ORDER BY id"
            ),
            {"w": WS, "d": destination},
        ).all()
    return [r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in rows]


# --- V-P2-16 -------------------------------------------------------------------------------------


def test_default_policy_is_read_reply_only_with_hourly_notice(
    engine: Engine, runtime: Runtime
) -> None:
    """V-P2-16: Task command under the default policy → zero execution, notice once per hour."""
    before = _counts(engine)
    res = _handle(engine, runtime, _msg(CHAT_A, 101, '/colab task create "From Telegram"'))
    assert res.handled and res.code == tg_policy.TELEGRAM_COMMANDS_DISABLED
    assert res.event_id is None and not res.throttled
    assert res.response_text and "read/reply only" in res.response_text
    after = _counts(engine)
    assert after[:2] == before[:2], "no Event and no Task from a Telegram command under default"
    assert after[2] == before[2] + 1  # exactly one notice row

    # a second command by the same user within the hour: still nothing executes, no new notice
    res2 = _handle(engine, runtime, _msg(CHAT_A, 102, "/colab task list"))
    assert res2.handled and res2.code == tg_policy.TELEGRAM_COMMANDS_DISABLED
    assert res2.throttled and res2.response_text is None
    assert _counts(engine) == after

    # another user gets their own (single) notice; an unlinked user too, with no identity lookup
    res3 = _handle(engine, runtime, _msg(CHAT_A, 103, "/colab task list", user=USER_UNLINKED))
    assert res3.code == tg_policy.TELEGRAM_COMMANDS_DISABLED and not res3.throttled
    assert _counts(engine) == (after[0], after[1], after[2] + 1)

    # next hour: the notice may be repeated once
    later = FixedClock(T0 + dt.timedelta(hours=1, minutes=1))
    res4 = _handle(engine, runtime, _msg(CHAT_A, 104, "/colab task list"), later)
    assert res4.code == tg_policy.TELEGRAM_COMMANDS_DISABLED and not res4.throttled
    assert _counts(engine) == (after[0], after[1], after[2] + 2)

    # the notices reach the Telegram chat through the transactional outbox
    fake = _deliver(engine, later)
    sent = [m.text for m in fake.messages.get(CHAT_A, [])]
    assert len(sent) == 3 and all(t and "read/reply only" in t for t in sent)
    payloads = _outbox_payloads(engine, f"telegram:{CHAT_A}")
    assert [p["reply_to_message_id"] for p in payloads] == [101, 103, 104]


def test_enabled_bridge_accepts_only_restricted_grammar(engine: Engine, runtime: Runtime) -> None:
    """V-P2-16: with commands allowed, only §7A.6 read verbs run; write verbs are refused."""
    before = _counts(engine)
    res = _handle(
        engine, runtime, _msg(CHAT_B, 201, '/colab task create "Write from TG" --criteria "done"')
    )
    assert res.handled and res.code == tg_policy.TELEGRAM_VERB_NOT_ALLOWED
    assert res.response_text and "task create" in res.response_text
    assert _counts(engine)[:2] == before[:2]

    res = _handle(engine, runtime, _msg(CHAT_B, 202, "/colab task list"))
    assert res.code == "OK" and res.response_text == "(no tasks)"
    assert _counts(engine)[:2] == before[:2]

    # invalid grammar → ephemeral error, zero side effects
    res = _handle(engine, runtime, _msg(CHAT_B, 203, "/colab task frobnicate t-1"))
    assert res.handled and res.code not in ("OK", tg_policy.TELEGRAM_VERB_NOT_ALLOWED)
    assert _counts(engine)[:2] == before[:2]

    # a Task created on the Mattermost side is readable from Telegram: show / list / doc show
    from server.api.dispatch import execute_command
    from server.application import tasks as tasks_app
    from server.identity.principals import Principal

    with Session(engine) as s:
        channel_uuid = s.execute(
            text("SELECT id FROM channels WHERE channel_id = 'chan-tc-b'")
        ).scalar_one()
    created = execute_command(
        runtime,
        Principal("acct-tc-active", str(ACTIVE), "human", "sha256:tc-active"),
        tasks_app.CreateTask(title="MM side task", channel_id=str(channel_uuid), domain="general"),
        idempotency_key="tc-mm-create",
        correlation_id="tc",
    )
    task_id = created.resource_id
    res = _handle(engine, runtime, _msg(CHAT_B, 204, f"/colab@agent_colab_bot task show {task_id}"))
    assert res.code == "OK" and res.resource_id == task_id
    assert (
        res.response_text and "MM side task" in res.response_text and "[OPEN]" in res.response_text
    )
    res = _handle(engine, runtime, _msg(CHAT_B, 205, "/colab task list --status OPEN"))
    assert res.code == "OK" and res.response_text and task_id in res.response_text
    res = _handle(engine, runtime, _msg(CHAT_B, 206, f"/colab doc show {task_id}"))
    assert res.handled and res.code == "DOCUMENT_NOT_FOUND"  # no Document yet, no side effect
    res = _handle(engine, runtime, _msg(CHAT_B, 207, "/colab approve show apr-none"))
    assert res.handled and res.code == "APPROVAL_NOT_FOUND"
    events, tasks, _ = _counts(engine)
    assert (events, tasks) == (before[0] + 1, before[1] + 1)  # only the Mattermost-side create

    # every reply is addressed to the originating message in the same chat
    payloads = _outbox_payloads(engine, f"telegram:{CHAT_B}")
    assert [p["reply_to_message_id"] for p in payloads] == [201, 202, 203, 204, 205, 206, 207]
    assert all(p["source"] == "telegram_command" for p in payloads)


def test_policy_opened_verb_executes_once_with_account_permissions(
    engine: Engine, runtime: Runtime
) -> None:
    """V-P2-16 (opened verb) + V-P2-20 (active / unlinked / suspended / under-privileged)."""
    before = _counts(engine)
    cmd = '/colab task create "Telegram task" --criteria "done" --risk LOW'
    res = _handle(engine, runtime, _msg(CHAT_C, 301, cmd))
    assert res.handled and res.code == "OK" and res.event_id and res.resource_id
    task_id = res.resource_id
    with Session(engine) as s:
        row = s.execute(
            text(
                "SELECT e.actor_account_id, t.title FROM events e JOIN tasks_projection t "
                "ON t.task_id = e.aggregate_id WHERE e.event_id = :e"
            ),
            {"e": res.event_id},
        ).first()
    assert row is not None and str(row[0]) == str(ACTIVE) and row[1] == "Telegram task"
    assert _counts(engine)[:2] == (before[0] + 1, before[1] + 1)

    # the same Telegram message replayed (duplicate update) → no second Event / Task
    replay = _handle(engine, runtime, _msg(CHAT_C, 301, cmd))
    assert replay.code == "OK" and replay.resource_id == task_id
    assert _counts(engine)[:2] == (before[0] + 1, before[1] + 1)

    # V-P2-20: same command by an unlinked user → link guidance, zero side effects
    res_u = _handle(engine, runtime, _msg(CHAT_C, 302, cmd, user=USER_UNLINKED))
    assert res_u.handled and res_u.code == TELEGRAM_USER_NOT_LINKED and res_u.event_id is None
    assert res_u.response_text and "link start" in res_u.response_text
    assert _counts(engine)[:2] == (before[0] + 1, before[1] + 1)

    # ... by a suspended link → stable error, zero side effects
    res_s = _handle(engine, runtime, _msg(CHAT_C, 303, cmd, user=USER_SUSPENDED))
    assert res_s.handled and res_s.code == EXTERNAL_IDENTITY_NOT_ACTIVE and res_s.event_id is None
    assert res_s.response_text and "suspended" in res_s.response_text
    assert _counts(engine)[:2] == (before[0] + 1, before[1] + 1)

    # ... by an active link whose Account lacks task.create → the command bus denies it
    res_r = _handle(engine, runtime, _msg(CHAT_C, 304, cmd, user=USER_READER))
    assert res_r.handled and res_r.code not in ("OK", TELEGRAM_USER_NOT_LINKED)
    assert res_r.event_id is None
    assert _counts(engine)[:2] == (before[0] + 1, before[1] + 1)
    # ... but that Account can still read
    res_show = _handle(
        engine, runtime, _msg(CHAT_C, 305, f"/colab task show {task_id}", user=USER_READER)
    )
    assert res_show.code == "OK" and res_show.resource_id == task_id

    # the unlinked / suspended users left no identity side effects either
    with Session(engine) as s:
        links = s.execute(
            text(
                "SELECT count(*) FROM external_identity_links WHERE provider_instance_id = :p "
                "AND external_user_id = :u"
            ),
            {"p": TG_PI_UUID, "u": USER_UNLINKED},
        ).scalar_one()
        status = s.execute(
            text(
                "SELECT status FROM external_identity_links WHERE provider_instance_id = :p "
                "AND external_user_id = :u"
            ),
            {"p": TG_PI_UUID, "u": USER_SUSPENDED},
        ).scalar_one()
    assert (links, status) == (0, "suspended")

    # replies were enqueued for every attempt and reach Telegram exactly once each
    fake = _deliver(engine, FixedClock(T0))
    texts = [m.text or "" for m in fake.messages.get(CHAT_C, [])]
    assert len(texts) == 5  # 301 (replay shares the dedupe key), 302, 303, 304, 305
    assert sum("not linked" in t for t in texts) == 1
    assert sum("not active" in t for t in texts) == 1


def test_non_commands_and_unbound_chats_are_left_to_the_relay(
    engine: Engine, runtime: Runtime
) -> None:
    before = _counts(engine)
    assert _handle(engine, runtime, _msg(CHAT_C, 401, "just chatting")).code == NOT_A_COMMAND
    assert _handle(engine, runtime, _msg(CHAT_C, 402, "/start")).code == NOT_A_COMMAND
    bot = _handle(engine, runtime, _msg(CHAT_C, 403, "/colab task list", is_bot=True))
    assert bot.code == NOT_A_COMMAND and not bot.handled
    unbound = _handle(engine, runtime, _msg(CHAT_UNBOUND, 404, "/colab task list"))
    assert unbound.code == BRIDGE_NOT_FOUND and not unbound.handled
    assert _counts(engine) == before
