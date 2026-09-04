"""The application owns its logging configuration, whatever its dependencies do (P7-02).

Constructing the MCP server calls ``logging.basicConfig`` with a ``RichHandler``, which is a fair
default for a script and wrong for a server: it seizes the root logger, so every record from
SQLAlchemy, uvicorn, alembic and psycopg is rendered to a console on stderr as a bare message,
without the JSON envelope, the correlation id or any structured field an aggregator reads.

It is invisible in normal testing because the application's own loggers set ``propagate = False``
and keep their own handlers — the access and command logs look perfectly correct while every
dependency's logging has been redirected. So this asserts the root logger specifically.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from server.config import Settings
from server.main import create_app
from server.observability.logs import JsonFormatter, reclaim_root_logging

pytestmark = pytest.mark.db


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(database_url=database_url, base_url="http://test")


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    """The root logger is global: put back whatever the rest of the suite had."""
    root = logging.getLogger()
    saved = list(root.handlers), root.level
    yield
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])


def _root_handlers() -> list[logging.Handler]:
    return list(logging.getLogger().handlers)


def test_building_the_app_leaves_the_root_logger_ours(settings: Settings) -> None:
    root = logging.getLogger()
    for handler in _root_handlers():
        root.removeHandler(handler)

    create_app(settings)

    handlers = _root_handlers()
    assert handlers, "the root logger must carry a handler after the app is built"
    assert all(isinstance(h.formatter, JsonFormatter) for h in handlers), (
        "a dependency has taken the root logger: "
        f"{[type(h).__module__ + '.' + type(h).__name__ for h in handlers]}"
    )


def test_a_dependency_record_is_emitted_as_json(settings: Settings, capsys) -> None:  # type: ignore[no-untyped-def]
    create_app(settings)
    capsys.readouterr()

    logging.getLogger("sqlalchemy.engine.probe").warning("dependency speaking")

    written = capsys.readouterr()
    assert '"logger": "sqlalchemy.engine.probe"' in written.err + written.out, (
        "a dependency's record did not reach the JSON handler"
    )


def test_reclaim_reports_what_it_removed() -> None:
    root = logging.getLogger()
    for handler in _root_handlers():
        root.removeHandler(handler)
    foreign = logging.StreamHandler()
    root.addHandler(foreign)

    removed = reclaim_root_logging()

    assert removed == ["logging.StreamHandler"], removed
    assert foreign not in _root_handlers()
    assert all(isinstance(h.formatter, JsonFormatter) for h in _root_handlers())
