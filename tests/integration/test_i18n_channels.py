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
