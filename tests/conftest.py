"""Shared fixtures. DB tests need AGENT_COLAB_TEST_DATABASE_URL (a fresh database is created)."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text

from server.db.engine import normalize_url, run_migrations

TEST_URL = os.environ.get("AGENT_COLAB_TEST_DATABASE_URL")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if TEST_URL:
        return
    skip = pytest.mark.skip(reason="AGENT_COLAB_TEST_DATABASE_URL not set")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """Create a disposable database from the maintenance URL, migrate to head, drop afterwards."""
    assert TEST_URL
    base = normalize_url(TEST_URL)
    maint = create_engine(base, isolation_level="AUTOCOMMIT")
    name = f"colab_t_{uuid.uuid4().hex[:12]}"
    with maint.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = base.rsplit("/", 1)[0] + f"/{name}"
    try:
        run_migrations(url)
        yield url
    finally:
        with maint.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        maint.dispose()
