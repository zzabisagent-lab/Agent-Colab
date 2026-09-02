"""V-P3-23 (P3-12): structured work message in the Task thread; a valid bot reply becomes one
work_result; duplicates ignored; malformed replies → ephemeral error, zero side effects;
unlinked posters ignored; secret-requiring work excludes the bot."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.agents.adapters.secret_support import supports_secret_handles
from server.agents.push_common import load_agent
from server.api.dispatch import Runtime
from server.application.authz import AllowAllAuthorizer
from server.channels.outbox import RecordingChannelProvider, drain_channels
from server.channels.telegram.bridge import MattermostPostView
from server.channels.work_messages import BotReplyIntake, deliver_to_bot
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore
from server.work import inbox
from server.work.state import WorkItemState

pytestmark = pytest.mark.db
WS, PI, CHANNEL = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
SERVICE, BOT_ACC = uuid.uuid4(), uuid.uuid4()
PI_ID, EXT, BOT_USER, AGENT = "mm:test:botadp", "ext-botadp", "mm-bot-user-1", "agent-bot-a"
TASK, ROOT = "task-botadp-1", "post-root-botadp"
T0 = dt.datetime(2026, 6, 2, 8, 0, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-botadp', 'b')"),
            {"i": WS},
        )
        for acc, name, typ in (
            (SERVICE, "acct-botadp-svc", "service"),
            (BOT_ACC, "acct-botadp-bot", "agent"),
        ):
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
                "base_url, team_or_bot_ref) VALUES (:i, :p, :w, 'mattermost', 'http://mm', 'team')"
            ),
            {"i": PI, "p": PI_ID, "w": WS},
        )
        s.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, provider_instance_id, "
                "external_channel_id, channel_type, display_name) "
                "VALUES (:i, 'chan-botadp', :w, :p, :e, 'work', 'bot channel')"
            ),
            {"i": CHANNEL, "w": WS, "p": PI, "e": EXT},
        )
        s.execute(
            text(
                "INSERT INTO tasks_projection (task_id, workspace_id, root_task_id, channel_id, "
                "title, domain, risk, status, created_at, updated_at) VALUES (:t, :w, :t, :c, "
                "'bot task', 'general', 'LOW', 'DELEGATED', :n, :n)"
            ),
            {"t": TASK, "w": WS, "c": CHANNEL, "n": T0},
        )
        s.execute(
            text(
                "INSERT INTO channel_posts (workspace_id, provider_instance_id, "
                "external_channel_id, subject_type, subject_id, role, dedupe_key, post_id, status) "
                "VALUES (:w, :p, :e, 'task', :t, 'card', :k, :root, 'sent')"
            ),
            {"w": WS, "p": PI_ID, "e": EXT, "t": TASK, "k": f"card:{PI_ID}:{TASK}", "root": ROOT},
        )
        s.execute(
            text(
                "INSERT INTO agents (id, agent_id, workspace_id, account_id, adapter_type, status, "
                "display_name, endpoint, delivery_modes) VALUES (:i, :g, :w, :a, 'mattermost_bot', "
                "'active', 'Bot A', CAST(:e AS jsonb), '[\"push\"]')"
            ),
            {
                "i": uuid.uuid4(),
                "g": AGENT,
                "w": WS,
                "a": BOT_ACC,
                "e": json.dumps(
                    {
                        "provider_instance_id": PI_ID,
                        "bot_user_id": BOT_USER,
                        "bot_username": "bot-a",
                    }
                ),
            },
        )
        s.execute(
            text(
                "INSERT INTO external_identity_links (id, link_id, provider_instance_id, "
                "external_user_id, account_id, verification_method, status, verified_at) "
                "VALUES (:i, 'link-botadp-1', :p, :u, :a, 'admin_approval', 'active', now())"
            ),
            {"i": uuid.uuid4(), "p": PI, "u": BOT_USER, "a": BOT_ACC},
        )
    yield eng
    eng.dispose()


def _runtime(engine: Engine, clock: FixedClock) -> Runtime:
    return Runtime(make_session_factory(engine), AllowAllAuthorizer(), None, clock, str(WS))


def _enqueue(
    s: Session, clock: FixedClock, key: str, handles: list[str] | None = None
) -> inbox.WorkItem:
    return inbox.enqueue(
        s,
        PostgresEventStore(s, clock=clock),
        workspace_id=str(WS),
        kind="task_assignment",
        agent_id=AGENT,
        payload={"title": "bot task"},
        deadline=clock.now() + dt.timedelta(hours=4),
        expected_result_schema="colab.work-result.v1",
        correlation_id=f"corr-{key}",
        idempotency_key=key,
        actor_account_id=str(SERVICE),
        clock=clock,
        task_id=TASK,
        secret_handles=handles,
    )


def _view(
    post_id: str, message: str, *, user: str = BOT_USER, root: str = ROOT
) -> MattermostPostView:
    return MattermostPostView(
        provider_instance_id=PI_ID,
        channel_ext_id=EXT,
        post_id=post_id,
        root_id=root,
        user_id=user,
        user_label="bot-a",
        message=message,
        user_is_bot=True,
    )


def _result_doc(wid: str, n: int = 1) -> str:
    doc: dict[str, Any] = {
        "schema_id": "colab.work-result.v1",
        "work_item_id": wid,
        "correlation_id": "corr-bot",
        "status": "SUCCEEDED",
        "result": {"n": n},
        "events": [],
        "artifacts": [],
        "usage_unavailable": {"reason": "ADAPTER_NO_METERING"},
    }
    return "done\n```json\n" + json.dumps(doc) + "\n```"


def _events(s: Session, wid: str) -> int:
    return int(
        s.execute(
            text("SELECT count(*) FROM events WHERE aggregate_id = :w"), {"w": wid}
        ).scalar_one()
    )


def test_work_message_and_reply_intake(engine: Engine) -> None:
    clock = FixedClock(T0)
    runtime = _runtime(engine, clock)
    intake = BotReplyIntake(runtime)
    mm = RecordingChannelProvider(prefix="mattermost")
    with Session(engine) as s, s.begin():
        store = PostgresEventStore(s, clock=clock)
        item = _enqueue(s, clock, "bot-wm-1")
        agent = load_agent(s, AGENT)
        assert agent is not None
        delivery_no, enqueued = deliver_to_bot(
            s, store, item, agent=agent, clock=clock, actor_account_id=str(SERVICE)
        )
        assert (delivery_no, enqueued) == (1, True)
        assert inbox.load(s, item.work_item_id).status is WorkItemState.DELIVERED
        drain_channels(s, {"mattermost": mm}, clock, str(WS))
        dest, payload = mm.calls[-1]
        assert dest == f"mattermost:{EXT}" and payload["root_id"] == ROOT
        assert "@bot-a work item" in payload["message"] and "```json" in payload["message"]
        assert (
            json.loads(payload["message"].split("```json\n")[1].split("\n```")[0])["work_item_id"]
            == item.work_item_id
        )
        row = s.execute(
            text(
                "SELECT status, root_post_id FROM channel_posts WHERE subject_type = 'work_item' "
                "AND subject_id = :w AND role = 'work_message'"
            ),
            {"w": item.work_item_id},
        ).one()
        assert row[0] == "sent" and row[1] == ROOT
        # valid reply → exactly one work result
        assert intake(s, clock, _view("p-reply-1", _result_doc(item.work_item_id))) is True
        assert intake.outcomes[-1].code == "RESULT_ACCEPTED"
        assert inbox.load(s, item.work_item_id).status is WorkItemState.RESULT_RECEIVED
        events_after = _events(s, item.work_item_id)
        # duplicate reply → ignored and audited, no Event
        assert intake(s, clock, _view("p-reply-2", _result_doc(item.work_item_id, 2))) is True
        assert intake.outcomes[-1].code == "DUPLICATE_RESULT_IGNORED"
        assert _events(s, item.work_item_id) == events_after
        kinds = [
            r[0]
            for r in s.execute(
                text("SELECT receipt_kind FROM work_item_receipts WHERE work_item_id = :w"),
                {"w": item.work_item_id},
            ).all()
        ]
        assert kinds.count("result") == 1 and kinds.count("duplicate_result") == 1
        # malformed reply → ephemeral error, audit, zero side effects
        before = _events(s, item.work_item_id)
        assert intake(s, clock, _view("p-reply-3", "oops\n```json\n{broken\n```")) is True
        assert intake.outcomes[-1].code == "WORK_RESULT_MALFORMED"
        assert _events(s, item.work_item_id) == before
        eph = s.execute(
            text(
                "SELECT payload->>'message' FROM delivery_outbox WHERE kind = "
                "'mattermost.ephemeral' AND dedupe_key = 'botreply-error:p-reply-3'"
            )
        ).scalar_one()
        assert eph.startswith("WORK_RESULT_MALFORMED")
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE action = 'work.bot_reply_rejected' "
                    "AND target_id = 'p-reply-3' AND workspace_id = :w"
                ),
                {"w": WS},
            ).scalar_one()
            == 1
        )
        # wrong thread → mismatch, zero side effects; unlinked poster → not interpreted at all
        assert (
            intake(s, clock, _view("p-reply-4", _result_doc(item.work_item_id), root="post-other"))
            is True
        )
        assert intake.outcomes[-1].code == "WORK_MESSAGE_THREAD_MISMATCH"
        assert (
            intake(s, clock, _view("p-reply-5", _result_doc(item.work_item_id), user="mm-human-9"))
            is False
        )
        assert intake(s, clock, _view("p-reply-6", "just chatting in the thread")) is False
        # a /colab command in the reply goes to the Command Router
        assert intake(s, clock, _view("p-reply-7", "/colab help")) is True
        assert intake.outcomes[-1].code != "COMMAND_PREFIX_MISSING"
        assert _events(s, item.work_item_id) == before


def test_secret_requiring_work_excludes_the_bot(engine: Engine) -> None:
    clock = FixedClock(T0 + dt.timedelta(hours=1))
    assert supports_secret_handles("mattermost_bot") is False  # routing eligibility input
    with Session(engine) as s, s.begin():
        store = PostgresEventStore(s, clock=clock)
        item = _enqueue(s, clock, "bot-secret-1", handles=["sh-00000000beef"])
        agent = load_agent(s, AGENT)
        assert agent is not None
        _no, enqueued = deliver_to_bot(
            s, store, item, agent=agent, clock=clock, actor_account_id=str(SERVICE)
        )
        assert enqueued is False
        assert inbox.load(s, item.work_item_id).status is WorkItemState.REJECTED
        assert (
            s.execute(
                text("SELECT count(*) FROM channel_posts WHERE subject_id = :w"),
                {"w": item.work_item_id},
            ).scalar_one()
            == 0
        )
        rej = s.execute(
            text(
                "SELECT payload->>'reason_code' FROM events WHERE aggregate_id = :w AND type = "
                "'WORK_ITEM_REJECTED'"
            ),
            {"w": item.work_item_id},
        ).scalar_one()
        assert rej == "CAPABILITY_UNSUPPORTED"
