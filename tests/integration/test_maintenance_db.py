"""P3 maintenance tick (gateway): sweeps run per Workspace in their own transactions; a Workspace
without a system service Account is skipped, never fatal; counters are returned."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.agents.maintenance import run_maintenance, workspace_ids
from server.api.dispatch import Runtime
from server.application.authz import AllowAllAuthorizer
from server.db.engine import make_engine, make_session_factory
from server.domain.clock import SystemClock

pytestmark = pytest.mark.db
WS = uuid.uuid4()


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-maint', 'm')"),
            {"i": WS},
        )
    yield eng
    eng.dispose()


def test_maintenance_runs_every_workspace_and_tolerates_missing_system_account(
    engine: Engine,
) -> None:
    rt = Runtime(make_session_factory(engine), AllowAllAuthorizer(), None, SystemClock(), str(WS))
    with Session(engine) as s:
        assert str(WS) in workspace_ids(s)
    counters = run_maintenance(rt)
    assert set(counters) == {"rerouted", "verifier_timeouts", "marked_offline", "errors"}
    assert counters["errors"] == 0  # SYSTEM_ACCOUNT_MISSING in ws-maint is skipped, not an error
    # with a system service Account the sweeps execute (nothing to do → zero counters)
    with Session(engine) as s, s.begin():
        s.execute(
            text(
                "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
                "VALUES (:i, 'acct-maint-system', :w, 'service', 'system')"
            ),
            {"i": uuid.uuid4(), "w": WS},
        )
    counters = run_maintenance(rt)
    assert counters["errors"] == 0
