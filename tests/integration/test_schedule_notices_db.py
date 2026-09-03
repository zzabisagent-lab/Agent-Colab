"""Channel notices for Schedule Runs (P5-07; V-P5-23): success, failure, skip and late notices
reach the Schedule's Mattermost channel as system events, in the channel's language, and a
Telegram Bridge relays them only when its content policy allows system events."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.schedules import execution
from server.schedules.notify import notices
from tests.integration.schedule_exec_fixture import Fixture
from tests.integration.schedule_seed import T0

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture
def fx(engine: Engine) -> Fixture:
    return Fixture.create(engine, f"ntc{uuid.uuid4().hex[:6]}", FixedClock(T0))


def _bind_channel(fx: Fixture, session: Session, *, language: str | None = None) -> str:
    """Give the Schedule's channel a Mattermost provider instance so notices can be delivered."""
    pi = uuid.uuid4()
    ext = f"ext-{fx.seed.tag}"
    session.execute(
        text(
            "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, provider, "
            "base_url, team_or_bot_ref) VALUES (:i, :p, :w, 'mattermost', 'http://mm', 'team')"
        ),
        {"i": pi, "p": f"mm:{fx.seed.tag}", "w": fx.seed.ws},
    )
    session.execute(
        text(
            "UPDATE channels SET provider_instance_id = :p, external_channel_id = :e, "
            "language = :l WHERE id = :c"
        ),
        {"p": pi, "e": ext, "l": language, "c": fx.seed.channel},
    )
    return ext


def _outbox(session: Session, run_id: str) -> list[dict[str, object]]:
    rows = session.execute(
        text(
            "SELECT kind, destination, payload FROM delivery_outbox "
            "WHERE dedupe_key LIKE :k ORDER BY id"
        ),
        {"k": f"notice:{run_id}:%"},
    ).all()
    return [
        {
            "kind": str(r[0]),
            "destination": str(r[1]),
            "payload": r[2] if isinstance(r[2], dict) else json.loads(r[2]),
        }
        for r in rows
    ]


def test_success_and_failure_notices_reach_the_channel(fx: Fixture, engine: Engine) -> None:
    """V-P5-23: start + result on success, start + failure on failure, one post per kind."""
    with Session(engine) as s, s.begin():
        ext = _bind_channel(fx, s)
        fx.schedule(s, "sch-ntc-ok")
        run = fx.run(s, "sch-ntc-ok", run_id="run-ntc-ok")
        outcome = execution.execute(run, fx.ctx(s))
        assert outcome.task_id
        fx.finish_task(s, str(outcome.task_id), "COMPLETED")
        execution.on_task_terminal(fx.ctx(s), str(outcome.task_id), "COMPLETED")
        kinds = [n["kind"] for n in notices(s, "run-ntc-ok")]
        assert kinds == ["start", "result"]
        posts = _outbox(s, "run-ntc-ok")
        assert [p["destination"] for p in posts] == [f"mattermost:{ext}"] * 2
        props = posts[0]["payload"]["props"]["agent_colab"]  # type: ignore[index]
        assert props["system_event"] is True and props["subject_type"] == "schedule_run"
        assert "run-ntc-ok" in str(posts[1]["payload"]["message"])  # type: ignore[index]

    with Session(engine) as s, s.begin():
        fx.schedule(s, "sch-ntc-fail")
        run = fx.run(s, "sch-ntc-fail", run_id="run-ntc-fail")
        outcome = execution.execute(run, fx.ctx(s))
        fx.finish_task(s, str(outcome.task_id), "CANCELLED")
        execution.on_task_terminal(fx.ctx(s), str(outcome.task_id), "CANCELLED")
        assert [n["kind"] for n in notices(s, "run-ntc-fail")] == ["start", "failure"]


def test_skip_notice_carries_the_stable_code(fx: Fixture, engine: Engine) -> None:
    """V-P5-23: a skipped Run posts one skip notice naming the untranslated code."""
    with Session(engine) as s, s.begin():
        _bind_channel(fx, s)
        fx.schedule(s, "sch-ntc-skip", concurrency="FORBID")
        fx.run(s, "sch-ntc-skip", run_id="run-ntc-active", status="RUNNING", task_id="task-x")
        run = fx.run(s, "sch-ntc-skip", run_id="run-ntc-skipped")
        execution.execute(run, fx.ctx(s))
        assert [n["kind"] for n in notices(s, "run-ntc-skipped")] == ["skip"]
        message = str(_outbox(s, "run-ntc-skipped")[0]["payload"]["message"])  # type: ignore[index]
        assert "SKIPPED_CONCURRENCY" in message  # codes are never translated


def test_notice_language_follows_the_channel(fx: Fixture, engine: Engine) -> None:
    """V-P5-23 with §7H: the channel's language selects the bundle; ids/codes stay untranslated."""
    with Session(engine) as s, s.begin():
        _bind_channel(fx, s, language="ko")
        fx.schedule(s, "sch-ntc-ko")
        run = fx.run(s, "sch-ntc-ko", run_id="run-ntc-ko")
        execution.execute(run, fx.ctx(s))
        message = str(_outbox(s, "run-ntc-ko")[0]["payload"]["message"])  # type: ignore[index]
        assert any("가" <= ch <= "힣" for ch in message)  # Korean bundle
        assert "run-ntc-ko" in message and "sch-ntc-ko" in message


def test_bridge_policy_decides_whether_a_notice_is_relayed(fx: Fixture, engine: Engine) -> None:
    """V-P5-23 (Bridge half): notices are system events, so a Bridge relays them only when its
    content policy allows ``system_event``."""
    from server.channels.telegram.bridge import Bridge, MattermostPostView

    with Session(engine) as s, s.begin():
        ext = _bind_channel(fx, s)
        fx.schedule(s, "sch-ntc-bridge")
        run = fx.run(s, "sch-ntc-bridge", run_id="run-ntc-bridge")
        execution.execute(run, fx.ctx(s))
        message = str(_outbox(s, "run-ntc-bridge")[0]["payload"]["message"])  # type: ignore[index]

        pi = s.execute(
            text("SELECT provider_instance_id FROM channels WHERE id = :c"), {"c": fx.seed.channel}
        ).scalar_one()
        for bridge_id, policy, expected in (
            ("br-ntc-off", {"text": True, "system_event": False}, False),
            ("br-ntc-on", {"text": True, "system_event": True}, True),
        ):
            s.execute(
                text(
                    "INSERT INTO telegram_bridges (id, bridge_id, workspace_id, channel_id, "
                    "provider_instance_id, telegram_chat_id, direction, thread_mode, status, "
                    "content_policy, created_by, created_at, updated_at) VALUES (:i, :b, :w, :c, "
                    ":p, :chat, 'bidirectional', 'general', 'enabled', CAST(:cp AS jsonb), :by, "
                    ":now, :now)"
                ),
                {
                    "i": uuid.uuid4(),
                    "b": bridge_id,
                    "w": fx.seed.ws,
                    "c": fx.seed.channel,
                    "p": pi,
                    "chat": f"-100{abs(hash(bridge_id)) % 10**9}",
                    "cp": json.dumps(policy),
                    "by": fx.seed.accounts[fx.seed.owner],
                    "now": fx.clock.now(),
                },
            )
            post = MattermostPostView(
                provider_instance_id=f"mm:{fx.seed.tag}",
                channel_ext_id=ext,
                post_id=f"post-{bridge_id}",
                root_id=None,
                user_id="bot",
                user_label="Agent-Colab",
                message=message,
                kind="system_event",
            )
            outcomes = Bridge(store=None).on_mattermost_post(s, fx.clock, post)
            relayed = [o for o in outcomes if o.bridge_id == bridge_id and o.accepted]
            assert bool(relayed) is expected, (bridge_id, [o.code for o in outcomes])
            s.execute(
                text("UPDATE telegram_bridges SET status = 'disabled' WHERE bridge_id = :b"),
                {"b": bridge_id},
            )
