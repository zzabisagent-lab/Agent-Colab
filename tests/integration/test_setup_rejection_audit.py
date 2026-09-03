"""V-P4-02 (F-P4-002): every Setup token rejection made before the database exists becomes exactly
one redacted ``setup.token_rejected`` AuditEvent once bootstrap reaches the DB step, including the
429 responses of a blocked source; retries never duplicate entries."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from tests.integration.setup_harness import Wizard, fresh_database

pytestmark = pytest.mark.db


@pytest.fixture
def empty_db() -> Iterator[str]:
    yield from fresh_database()


def test_pre_db_rejections_become_audit_events(tmp_path: Path, empty_db: str) -> None:
    wiz = Wizard(tmp_path)
    try:
        token = wiz.token()
        wiz.configure_all(empty_db)
        assert wiz.preflight()["ok"]
        # six rejections before any database exists: 5 invalid (403) + the 6th blocked (429)
        codes = []
        for _ in range(6):
            r = wiz.bootstrap("not-the-token-" + "x" * 20)
            codes.append((r.status_code, r.json()["code"]))
        assert [c for c, _ in codes][:5] == [403] * 5 and codes[5][0] == 429
        assert codes[5][1] == "SETUP_TOKEN_BLOCKED"
        local = wiz.service.local_document()
        assert len(local["rejection_log"]) == 6 and all(
            not e["audited"] for e in local["rejection_log"]
        )
        # the blocked source cannot bootstrap; a fresh source (loopback shim ip) finishes setup
        wiz.client.__exit__(None, None, None)
        wiz.client = type(wiz.client)(type(wiz.client.app)(wiz.app, ("127.0.0.2", 5556)))
        wiz.client.__enter__()
        r = wiz.bootstrap(token)
        assert r.status_code == 200 and r.json()["state"] == "LOCKED", r.text
        engine = create_engine(empty_db)
        with engine.connect() as c:
            rows = c.execute(
                text(
                    "SELECT redacted_metadata->>'id', error_code FROM audit_events "
                    "WHERE action = 'setup.token_rejected' ORDER BY id"
                )
            ).all()
        engine.dispose()
        assert len(rows) == 6 and len({r[0] for r in rows}) == 6
        assert [r[1] for r in rows].count("SETUP_TOKEN_BLOCKED") == 1
        # the sealed store keeps only the LOCKED marker: rejections now live in the DB alone
        assert wiz.service.local_document().get("rejection_log", []) == []
        # migration is idempotent: re-running it adds nothing
        with wiz.service.session_factory() as s, s.begin():  # type: ignore[misc]
            assert wiz.service.migrate_rejections_to_audit(s) == 0
    finally:
        wiz.close()
