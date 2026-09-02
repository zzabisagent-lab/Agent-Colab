"""V-P2-25: one root post edited in place, one thread reply per transition, progress coalesced at
10 s, bodies over 16k linked as Artifacts, sub-Task link card — driven through the command bus."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.api.dispatch import Runtime, execute_command
from server.application import tasks as t
from server.application.authz import AllowAllAuthorizer
from server.channels.outbox import RecordingChannelProvider, drain_channels
from server.channels.task_cards import bind_delivered_cards
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import FixedClock
from server.identity.principals import Principal

pytestmark = pytest.mark.db

WS = uuid.uuid4()
PI = uuid.uuid4()
CHANNEL = uuid.uuid4()
HUMAN = uuid.uuid4()
AGENT = uuid.uuid4()
EXT = "mmchan-cards-1"
PI_TEXT = "mm:test:cards"
CRITERIA = ({"statement": "done", "check_type": "evidence", "required": True},)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-cards', 'cards')"
            ),
            {"i": WS},
        )
        for acc, name, typ in ((HUMAN, "acct-cards-h", "human"), (AGENT, "acct-cards-a", "agent")):
            c.execute(
                text(
                    "INSERT INTO accounts "
                    "(id, account_id, workspace_id, account_type, display_name) "
                    "VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc, "a": name, "w": WS, "t": typ},
            )
        c.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, "
                "provider, base_url, team_or_bot_ref) "
                "VALUES (:i, :p, :w, 'mattermost', 'http://mm', 'colab-test')"
            ),
            {"i": PI, "p": PI_TEXT, "w": WS},
        )
        c.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, provider_instance_id, "
                "external_channel_id, "
                "channel_type, display_name) VALUES (:i, 'chan-cards', :w, :p, :e, 'work', 'cards')"
            ),
            {"i": CHANNEL, "w": WS, "p": PI, "e": EXT},
        )
    yield eng
    eng.dispose()


def _principal(acc: uuid.UUID, name: str, typ: str) -> Principal:
    return Principal(name, str(acc), typ, f"sha256:{name}")


def _runtime(engine: Engine, clock: FixedClock) -> Runtime:
    return Runtime(make_session_factory(engine), AllowAllAuthorizer(), None, clock, str(WS))


def _drain(engine: Engine, clock: FixedClock, provider: RecordingChannelProvider) -> None:
    with Session(engine) as s, s.begin():
        drain_channels(s, {"mattermost": provider}, clock, str(WS))
        bind_delivered_cards(s)


def test_task_card_thread_rules(engine: Engine) -> None:
    clock = FixedClock(dt.datetime(2026, 6, 1, tzinfo=dt.UTC))
    rt = _runtime(engine, clock)
    human = _principal(HUMAN, "acct-cards-h", "human")
    agent = _principal(AGENT, "acct-cards-a", "agent")
    provider = RecordingChannelProvider()

    def run(principal: Principal, cmd: t.Command, key: str) -> str:  # type: ignore[name-defined]
        res = execute_command(rt, principal, cmd, idempotency_key=key, correlation_id="corr-cards")
        clock.advance(dt.timedelta(seconds=1))
        _drain(engine, clock, provider)
        return res.resource_id

    task_id = run(
        human, t.CreateTask("Card task", str(CHANNEL), "research", "LOW", criteria=CRITERIA), "c1"
    )
    with Session(engine) as s:
        bound = s.execute(
            text("SELECT root_post_id FROM thread_bindings WHERE subject_id = :t"), {"t": task_id}
        ).scalar()
    assert bound is not None, "card delivered and bound to the Task"
    # 10 transitions
    run(human, t.DelegateTask(task_id, "acct-cards-a"), "c2")
    run(agent, t.AcceptTask(task_id), "c3")
    run(agent, t.StartTask(task_id), "c4")
    # three progress reports inside one 10-second window are coalesced into a single reply
    for key, summary in (("c5", "p1"), ("c6", "p2"), ("c7", "p3")):
        execute_command(
            rt, agent, t.ReportProgress(task_id, summary), idempotency_key=key, correlation_id="x"
        )
        clock.advance(dt.timedelta(seconds=2))
    clock.advance(dt.timedelta(seconds=11))
    _drain(engine, clock, provider)
    run(agent, t.MarkWaiting(task_id, "EXTERNAL"), "c8")
    run(agent, t.StartTask(task_id), "c9")
    long_summary = "x" * 16_500
    run(agent, t.ReportProgress(task_id, long_summary), "c10")
    clock.advance(dt.timedelta(seconds=11))
    _drain(engine, clock, provider)
    sub = run(human, t.CreateSubtask(task_id, "Sub", "research", "LOW", criteria=CRITERIA), "c11")
    run(human, t.RequestCancel(task_id, "CHANGED_MIND"), "c12")
    run(human, t.CancelTask(task_id, "CHANGED_MIND"), "c13")

    cards = [
        c
        for c in provider.calls
        if c[1].get("props", {}).get("agent_colab", {}).get("subject_id") == task_id
        and "post_id" not in c[1]
        and not c[1].get("root_id")
    ]
    patches = [c for c in provider.calls if "post_id" in c[1]]
    replies = [
        c for c in provider.calls if c[1].get("root_id") == bound and c[1].get("props") is None
    ]
    assert len(cards) == 1, "exactly one root post per Task"
    assert len(patches) >= 10, "every transition edits the card in place"
    texts = [r[1]["message"] for r in replies]
    assert sum("Progress (3 updates)" in m and "p1 | p2 | p3" in m for m in texts) == 1, texts
    assert not any(len(m) > 16_000 for m in texts) and any("stored as Artifact" in m for m in texts)
    with Session(engine) as s:
        arts = s.execute(
            text(
                "SELECT count(*) FROM artifact_links WHERE subject_id = :t "
                "AND relation = 'thread_body'"
            ),
            {"t": task_id},
        ).scalar_one()
        s.execute(
            text(
                "SELECT count(*) FROM events WHERE aggregate_type = 'task' AND aggregate_id = :t "
                "AND type NOT IN ('TASK_CREATED', 'TASK_PROGRESS_REPORTED')"
            ),
            {"t": task_id},
        ).scalar_one()
    assert arts == 1
    # exactly one reply per non-progress transition
    # (delegate, accept, start, waiting, start, cancel requested, cancelled)
    non_progress = [m for m in texts if not m.startswith("Progress")]
    assert len(non_progress) == 7
    link_cards = [
        c for c in provider.calls if c[1].get("props", {}).get("agent_colab", {}).get("link_card")
    ]
    assert (
        len(link_cards) == 1
        and link_cards[0][1]["root_id"] == bound
        and sub in link_cards[0][1]["message"]
    )
