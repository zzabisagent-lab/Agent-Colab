"""V-P3-26 (P3-15): usage reporting conformance — result/heartbeat usage from adapters is
normalized into usage_records (cost_units from pricing, estimated for unknown models), a missing
report is recorded as unavailable with a reason, and the usage_unavailable ratio is measured."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.usage.conformance import (
    normalize_usage,
    record_heartbeat_usage,
    record_result_usage,
    usage_unavailable_ratio,
)
from server.usage.versions import activate_from_file

pytestmark = pytest.mark.db
WS, ACC = uuid.uuid4(), uuid.uuid4()
AGENT = "agent-usage-conf"
T0 = dt.datetime(2026, 9, 1, 8, 0, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-usc', 'usc')"),
            {"i": WS},
        )
        s.execute(
            text(
                "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
                "VALUES (:i, 'acct-usc-a', :w, 'agent', 'usc')"
            ),
            {"i": ACC, "w": WS},
        )
        s.execute(
            text(
                "INSERT INTO agents (id, agent_id, workspace_id, account_id, adapter_type, status, "
                "display_name) VALUES (:i, :g, :w, :a, 'mcp', 'active', :g)"
            ),
            {"i": uuid.uuid4(), "g": AGENT, "w": WS, "a": ACC},
        )
        activate_from_file(s, activated_by=str(ACC))
    yield eng
    eng.dispose()


def test_normalize_usage_cases() -> None:
    ok = normalize_usage(
        {
            "usage": {
                "model": "m",
                "input_tokens": 1,
                "output_tokens": 2,
                "tool_calls": 0,
                "wall_time_ms": 3,
            }
        }
    )
    assert ok.conformant and ok.usage and ok.unavailable_reason is None
    reason = normalize_usage({"usage_unavailable": {"reason": "ADAPTER_NO_METERING"}})
    assert reason.conformant and reason.unavailable_reason == "ADAPTER_NO_METERING"
    missing = normalize_usage({"result": {}})
    assert not missing.conformant and missing.unavailable_reason == "ADAPTER_NO_METERING"
    bad = normalize_usage({"usage": {"model": "m", "input_tokens": -1}})
    assert not bad.conformant and bad.unavailable_reason == "ADAPTER_METERING_FAILED"


def test_results_heartbeats_and_ratio(engine: Engine) -> None:
    clock = FixedClock(T0)
    usage = {
        "model": "gpt-x-unknown",
        "input_tokens": 1000,
        "output_tokens": 500,
        "tool_calls": 2,
        "wall_time_ms": 1200,
    }
    with Session(engine) as s, s.begin():
        measured = record_result_usage(
            s,
            workspace_id=str(WS),
            account_id=str(ACC),
            agent_id=AGENT,
            work_item_id="wi-usc-1",
            payload={"usage": usage},
            clock=clock,
        )
        assert measured is not None and measured.source == "estimated" and measured.cost_units > 0
        reported = record_result_usage(
            s,
            workspace_id=str(WS),
            account_id=str(ACC),
            agent_id=AGENT,
            work_item_id="wi-usc-2",
            payload={"usage": {**usage, "cost_units": 4242}},
            clock=clock,
        )
        assert (
            reported is not None and reported.source == "reported" and reported.cost_units == 4242
        )
        unavailable = record_result_usage(
            s,
            workspace_id=str(WS),
            account_id=str(ACC),
            agent_id=AGENT,
            work_item_id="wi-usc-3",
            payload={"usage_unavailable": {"reason": "ADAPTER_NO_METERING"}},
            clock=clock,
        )
        assert unavailable is not None and unavailable.source == "unavailable"
        assert unavailable.unavailable_reason == "ADAPTER_NO_METERING"
        silent = record_result_usage(
            s,
            workspace_id=str(WS),
            account_id=str(ACC),
            agent_id=AGENT,
            work_item_id="wi-usc-4",
            payload={"result": {}},
            clock=clock,
        )
        assert silent is not None and silent.source == "unavailable"
        beat = record_heartbeat_usage(
            s,
            workspace_id=str(WS),
            account_id=str(ACC),
            agent_id=AGENT,
            usage_since_last={
                "model": "m",
                "input_tokens": 5,
                "output_tokens": 5,
                "tool_calls": 0,
                "wall_time_ms": 10,
            },
            clock=clock,
        )
        assert beat.cost_units >= 0 and beat.source in ("computed", "estimated")
    with Session(engine) as s:
        ratio = usage_unavailable_ratio(
            s, AGENT, T0 - dt.timedelta(hours=1), T0 + dt.timedelta(hours=1), workspace_id=str(WS)
        )
        assert ratio.total == 5 and ratio.unavailable == 2 and ratio.ratio == pytest.approx(0.4)
        assert ratio.estimated >= 1
        rows = s.execute(
            text("SELECT count(*) FROM usage_records WHERE agent_id = :a AND workspace_id = :w"),
            {"a": AGENT, "w": WS},
        ).scalar_one()
        assert rows == 5
