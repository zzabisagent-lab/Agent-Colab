"""V-P2-30: instance default ko, channel override en → ephemeral errors and cards in the
configured language; Event types, error codes and IDs are never translated."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text

from server.api.dispatch import Runtime
from server.application.authz import AllowAllAuthorizer
from server.channels.router import SlashRequest, route
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import FixedClock
from server.events.store import AppendRequest

pytestmark = pytest.mark.db

WS, PI = uuid.uuid4(), uuid.uuid4()
PI_TEXT = "mm:test:i18n"


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-i18n', 'i')"),
            {"i": WS},
        )
        c.execute(
            text(
                "INSERT INTO provider_instances (id, provider_instance_id, workspace_id, provider, "
                "base_url, team_or_bot_ref) VALUES (:i, :p, :w, 'mattermost', 'http://mm', 'team')"
            ),
            {"i": PI, "p": PI_TEXT, "w": WS},
        )
        for ext, lang in (("ext-ko-default", None), ("ext-en-override", "en")):
            c.execute(
                text(
                    "INSERT INTO channels (id, channel_id, workspace_id, provider_instance_id, "
                    "external_channel_id, channel_type, display_name, language) "
                    "VALUES (:i, :c, :w, :p, :e, 'work', :e, :l)"
                ),
                {"i": uuid.uuid4(), "c": f"chan-{ext}", "w": WS, "p": PI, "e": ext, "l": lang},
            )
    yield eng
    eng.dispose()


def _req(ext: str, text_in: str) -> SlashRequest:
    return SlashRequest(
        provider_instance_id=PI_TEXT,
        team_id="team",
        channel_id=ext,
        user_id="mm-user-i18n",
        user_name="someone",
        command="/colab",
        text=text_in,
        trigger_id=uuid.uuid4().hex,
        response_url=None,
        post_id=None,
        root_id=None,
    )


def test_language_per_channel_with_instance_default(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_COLAB_DEFAULT_LANGUAGE", "ko")
    rt = Runtime(
        make_session_factory(engine),
        AllowAllAuthorizer(),
        None,
        FixedClock.__new__(FixedClock),
        str(WS),
    )
    import datetime as dt

    rt.clock = FixedClock(dt.datetime(2026, 7, 1, tzinfo=dt.UTC))
    ko = route(rt, _req("ext-ko-default", "task"), rt.clock)
    en = route(rt, _req("ext-en-override", "task"), rt.clock)
    assert ko.response_type == en.response_type == "ephemeral"
    assert ko.code == en.code  # stable error code identical across languages
    assert ko.text != en.text
    assert "동사" in ko.text or "명령" in ko.text
    assert ko.code.isupper() and "_" in ko.code  # codes are never translated
    assert os.environ["AGENT_COLAB_DEFAULT_LANGUAGE"] == "ko"


def test_task_cards_follow_channel_language(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cards/thread replies in the channel language; Event types, IDs untranslated."""
    import datetime as dt

    from server.channels.task_cards import render_task_event

    monkeypatch.setenv("AGENT_COLAB_DEFAULT_LANGUAGE", "ko")
    now = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
    actor = uuid.uuid4()
    texts: dict[str, str] = {}
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
                "VALUES (:i, 'acct-i18n-svc', :w, 'service', 'svc')"
            ),
            {"i": actor, "w": WS},
        )
    for ext in ("ext-ko-default", "ext-en-override"):
        task_id = f"task-i18n-{ext}"
        with engine.begin() as c:
            chan = c.execute(
                text("SELECT id FROM channels WHERE external_channel_id = :e"), {"e": ext}
            ).scalar_one()
            c.execute(
                text(
                    "INSERT INTO tasks_projection (task_id, workspace_id, "
                    "root_task_id, channel_id, "
                    "title, domain, risk, status, created_at, updated_at) VALUES (:t, :w, :t, :c, "
                    "'i18n card', 'general', 'LOW', 'OPEN', :n, :n)"
                ),
                {"t": task_id, "w": WS, "c": chan, "n": now},
            )
        from sqlalchemy.orm import Session

        rt = Runtime(
            make_session_factory(engine), AllowAllAuthorizer(), None, FixedClock(now), str(WS)
        )
        with Session(engine) as s, s.begin():
            appended = rt.store_for(s).append(
                AppendRequest(
                    workspace_id=str(WS),
                    aggregate_type="task",
                    aggregate_id=task_id,
                    type="TASK_CREATED",
                    actor_account_id=str(actor),
                    correlation_id=f"corr-{ext}",
                    idempotency_scope="task:create",
                    idempotency_key=task_id,
                    payload={
                        "task_id": task_id,
                        "root_task_id": task_id,
                        "channel_id": f"chan-{ext}",
                        "title": "i18n card",
                        "domain": "general",
                        "risk": "LOW",
                    },
                )
            )
            render_task_event(
                s,
                workspace_id=str(WS),
                actor_uuid=str(actor),
                event={
                    "event_id": appended.event_id,
                    "type": "TASK_CREATED",
                    "aggregate_id": task_id,
                    "aggregate_seq": appended.aggregate_seq,
                    "task_id": task_id,
                    "payload": {"task_id": task_id, "title": "i18n card"},
                },
                now=now,
            )
            rows = s.execute(
                text(
                    "SELECT payload::text FROM delivery_outbox WHERE destination = :d "
                    "AND workspace_id = :w"
                ),
                {"d": f"mattermost:{ext}", "w": WS},
            ).all()
        assert rows, ext
        texts[ext] = " ".join(str(r[0]) for r in rows)

    def korean(s: str) -> bool:
        return any("\uac00" <= ch <= "\ud7a3" for ch in s)

    ko_text, en_text = texts["ext-ko-default"], texts["ext-en-override"]
    # status codes (enums) stay untranslated in both languages
    assert '"status": "OPEN"' in ko_text and '"status": "OPEN"' in en_text
    assert "task-i18n-ext-ko-default" in ko_text  # IDs untranslated
    assert korean(ko_text) and not korean(en_text)
