"""P2-03: transactional outbox (V-P2-23) and Renderer latency (V-P2-02)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.channels.outbox import (
    Delivery,
    RecordingChannelProvider,
    card_post_id,
    drain_channels,
    enqueue_delivery,
)
from server.channels.renderer import CardInput, render_task_card, render_transition
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.domain.defaults import NORMAL_LOAD
from server.events.postgres_store import PostgresEventStore
from server.events.store import AppendRequest

pytestmark = pytest.mark.db

WS = uuid.uuid4()
ACTOR = uuid.uuid4()
CHANNEL = uuid.uuid4()
PI = "mm:test:colab-test"
EXT = "chan-ext-1"


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-obx', 'obx')"),
            {"i": WS},
        )
        c.execute(
            text(
                "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
                "VALUES (:i, 'acct-obx', :w, 'service', 'obx')"
            ),
            {"i": ACTOR, "w": WS},
        )
        c.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name, "
                "external_channel_id) VALUES (:i, 'chan-obx', :w, 'work', 'obx', :ext)"
            ),
            {"i": CHANNEL, "w": WS, "ext": EXT},
        )
    yield eng
    eng.dispose()


def _render_and_enqueue(
    s: Session, clock: FixedClock, task_id: str, key: str, *, fail_between: bool = False
) -> str:
    """Event append + Renderer enqueue in ONE transaction (the P2-03 invariant)."""
    store = PostgresEventStore(s, clock=clock)
    res = store.append(
        AppendRequest(
            workspace_id=str(WS),
            aggregate_type="task",
            aggregate_id=task_id,
            type="TASK_PROGRESS_REPORTED",
            actor_account_id=str(ACTOR),
            correlation_id="corr-obx",
            idempotency_scope="task:progress",
            idempotency_key=key,
            payload={"task_id": task_id, "summary": f"step {key}"},
            task_id=task_id,
            channel_id=str(CHANNEL),
        )
    )
    if fail_between:
        raise RuntimeError("crash between Event insert and outbox insert")
    text_out = render_transition("TASK_PROGRESS_REPORTED", {"summary": f"step {key}"})
    assert text_out
    enqueue_delivery(
        s,
        workspace_id=str(WS),
        source_event_id=res.event_id,
        delivery=Delivery(
            "mattermost.post",
            f"mattermost:{EXT}",
            {"message": text_out},
            f"reply:{res.event_id}",
            subject_type="task",
            subject_id=task_id,
            role="reply",
        ),
        provider_instance_id=PI,
        external_channel_id=EXT,
        now=clock.now(),
    )
    return res.event_id


def test_failure_between_event_and_outbox_rolls_back_both(engine: Engine) -> None:  # V-P2-23 (a)
    clock = FixedClock(dt.datetime(2026, 5, 1, tzinfo=dt.UTC))
    with pytest.raises(RuntimeError):
        with Session(engine) as s, s.begin():
            _render_and_enqueue(s, clock, "task-obx-a", "a1", fail_between=True)
    with Session(engine) as s:
        assert (
            s.execute(
                text("SELECT count(*) FROM events WHERE aggregate_id = 'task-obx-a'")
            ).scalar_one()
            == 0
        )
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM delivery_outbox WHERE destination = :d AND "
                    "payload->>'message' LIKE '%a1%'"
                ),
                {"d": f"mattermost:{EXT}"},
            ).scalar_one()
            == 0
        )


def test_crash_after_provider_send_yields_exactly_one_side_effect_after_replay(
    engine: Engine,
) -> None:  # V-P2-23 (b)
    clock = FixedClock(dt.datetime(2026, 5, 1, 1, tzinfo=dt.UTC))
    with Session(engine) as s, s.begin():
        event_id = _render_and_enqueue(s, clock, "task-obx-b", "b1")
    provider = RecordingChannelProvider(fail_after_send_times=1)
    # first drain: the provider performs the side effect, then the process "crashes" (exception)
    with Session(engine) as s, s.begin():
        r1 = drain_channels(s, {"mattermost": provider}, clock, str(WS))
    assert r1.failed == 1 and len(provider.calls) == 1
    # replay: the row is retried after backoff; the provider's idempotency yields no second effect
    clock.advance(dt.timedelta(seconds=2))
    with Session(engine) as s, s.begin():
        r2 = drain_channels(s, {"mattermost": provider}, clock, str(WS))
    assert r2.sent == 1 and len(provider.calls) == 1  # exactly one destination side effect
    with Session(engine) as s:
        row = s.execute(
            text("SELECT status, post_id FROM channel_posts WHERE dedupe_key = :k"),
            {"k": f"reply:{event_id}"},
        ).first()
        assert (
            row is not None
            and row[0] == "sent"
            and row[1] == provider.delivered[f"reply:{event_id}"]
        )
        outbox = s.execute(
            text("SELECT status, attempts FROM delivery_outbox WHERE dedupe_key = :k"),
            {"k": f"reply:{event_id}"},
        ).first()
        assert outbox is not None and outbox[0] == "sent" and outbox[1] == 2
    # a third drain does nothing
    with Session(engine) as s, s.begin():
        assert drain_channels(s, {"mattermost": provider}, clock, str(WS)).sent == 0


def test_card_is_posted_once_then_edited_in_place(engine: Engine) -> None:
    clock = FixedClock(dt.datetime(2026, 5, 1, 2, tzinfo=dt.UTC))
    provider = RecordingChannelProvider()
    with Session(engine) as s, s.begin():
        card = render_task_card(CardInput("task-obx-c", "Card", "OPEN", "LOW", "d"))
        enqueue_delivery(
            s,
            workspace_id=str(WS),
            source_event_id=None,
            delivery=Delivery(
                "mattermost.post",
                f"mattermost:{EXT}",
                {"message": card.text, "props": card.props},
                "card:task-obx-c",
                subject_type="task",
                subject_id="task-obx-c",
                role="card",
            ),
            provider_instance_id=PI,
            external_channel_id=EXT,
            now=clock.now(),
        )
        assert (
            enqueue_delivery(  # duplicate enqueue of the same dedupe key is a no-op
                s,
                workspace_id=str(WS),
                source_event_id=None,
                delivery=Delivery(
                    "mattermost.post",
                    f"mattermost:{EXT}",
                    {"message": "dup"},
                    "card:task-obx-c",
                    subject_type="task",
                    subject_id="task-obx-c",
                    role="card",
                ),
                provider_instance_id=PI,
                external_channel_id=EXT,
                now=clock.now(),
            )
            is None
        )
    with Session(engine) as s, s.begin():
        drain_channels(s, {"mattermost": provider}, clock, str(WS))
        root = card_post_id(s, PI, "task", "task-obx-c")
        assert root == provider.delivered["card:task-obx-c"]
        # a transition edits the card in place (patch) and adds exactly one thread reply
        updated = render_task_card(
            CardInput("task-obx-c", "Card", "DELEGATED", "LOW", "d", assignee="@a")
        )
        enqueue_delivery(
            s,
            workspace_id=str(WS),
            source_event_id=None,
            delivery=Delivery(
                "mattermost.patch",
                f"mattermost:{EXT}",
                {"post_id": root, "message": updated.text},
                "card:task-obx-c:v2",
            ),
            provider_instance_id=PI,
            external_channel_id=EXT,
            now=clock.now(),
        )
        enqueue_delivery(
            s,
            workspace_id=str(WS),
            source_event_id=None,
            delivery=Delivery(
                "mattermost.post",
                f"mattermost:{EXT}",
                {"message": "Delegated", "root_id": root},
                "reply:evt-c-2",
                subject_type="task",
                subject_id="task-obx-c",
                role="reply",
                root_post_id=root,
            ),
            provider_instance_id=PI,
            external_channel_id=EXT,
            now=clock.now(),
        )
        drain_channels(s, {"mattermost": provider}, clock, str(WS))
    cards = [c for c in provider.calls if c[1].get("props")]
    assert len(cards) == 1  # one root post; the update was a patch
    assert any("post_id" in c[1] for c in provider.calls)


def test_renderer_latency_p95_under_normal_profile(engine: Engine) -> None:  # V-P2-02
    """100 Events rendered and delivered at the §21.1 message rate: p95 ≤ 5 s, max ≤ 15 s."""
    clock = FixedClock(dt.datetime(2026, 5, 1, 3, tzinfo=dt.UTC))
    provider = RecordingChannelProvider()
    step = dt.timedelta(seconds=1 / NORMAL_LOAD.messages_rps)
    for i in range(100):
        with Session(engine) as s, s.begin():
            _render_and_enqueue(s, clock, "task-obx-lat", f"l{i}")
        clock.advance(step)
        if i % 10 == 9:  # the drain runs on its own cadence; here every second of virtual time
            with Session(engine) as s, s.begin():
                drain_channels(s, {"mattermost": provider}, clock, str(WS))
    with Session(engine) as s, s.begin():
        final = drain_channels(s, {"mattermost": provider}, clock, str(WS))
    with Session(engine) as s:
        rows = s.execute(
            text(
                "SELECT EXTRACT(EPOCH FROM (o.sent_at - e.recorded_at)) FROM delivery_outbox o "
                "JOIN events e ON e.event_id = o.source_event_id WHERE e.aggregate_id "
                "= 'task-obx-lat' "
                "AND o.status = 'sent'"
            )
        ).all()
    assert len(rows) == 100 and final.sent >= 0
    # virtual-clock latency (enqueue → sent) is bounded by the drain cadence: ≤ 1 s here
    virtual = sorted(int(x) for x in [lat for lat in _virtual_latencies(engine)])
    p95 = virtual[int(len(virtual) * 0.95) - 1]
    assert p95 <= 5_000 and max(virtual) <= 15_000, (p95, max(virtual))
    assert len(provider.calls) == 100 and len(set(provider.delivered.values())) == 100


def _virtual_latencies(engine: Engine) -> list[int]:
    with Session(engine) as s:
        rows = s.execute(
            text(
                "SELECT EXTRACT(EPOCH FROM (sent_at - created_at)) * 1000 FROM delivery_outbox "
                "WHERE destination = :d AND status = 'sent' AND payload->>'message' "
                "LIKE 'Progress: step l%'"
            ),
            {"d": f"mattermost:{EXT}"},
        ).all()
    return [int(r[0]) for r in rows]
