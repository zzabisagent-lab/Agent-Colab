"""Verifier-authored negative test for the V-P3-15 brainstorm-turn branch."""

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from server.agents import limits
from server.domain.clock import FixedClock
from tests.conftest import database_url
from tests.integration.test_agent_limits import T0, WS, _setup_agent, engine


def test_turn_limit_rejects_and_audits_without_event_side_effect(engine):
    now = T0 + dt.timedelta(days=1)
    _setup_agent(engine, FixedClock(now))
    with Session(engine) as session, session.begin():
        session.execute(
            text("UPDATE agents SET limits = limits || '{\"brainstorm_turns\": 1}'::jsonb "
                 "WHERE agent_id = 'agent-lim-1'")
        )
        session.execute(
            text("INSERT INTO work_items (work_item_id, workspace_id, kind, agent_id, "
                 "brainstorm_id, correlation_id, deadline, payload, expected_result_schema, "
                 "idempotency_key, status, created_at, updated_at) VALUES "
                 "('wi-verifier-turn-limit', :ws, 'brainstorm_turn', 'agent-lim-1', "
                 "'bs-verifier', 'corr-verifier', :deadline, '{}', "
                 "'colab.brainstorm-contribution.v1', 'verifier-turn-limit', 'QUEUED', :now, :now)"),
            {"ws": WS, "deadline": now + dt.timedelta(hours=1), "now": now},
        )
    with Session(engine) as session:
        events_before = session.execute(
            text("SELECT count(*) FROM events WHERE workspace_id = :ws"), {"ws": WS}
        ).scalar_one()
    with Session(engine) as session, pytest.raises(limits.AgentLimitExceededError) as exc:
        limits.enforce_limits(
            session,
            "agent-lim-1",
            "brainstorm_turn",
            FixedClock(now),
            workspace_id=WS,
            correlation_id="corr-verifier-limit",
            count_request=False,
        )
    assert exc.value.code == "AGENT_LIMIT_EXCEEDED"
    assert exc.value.check.limit == "brainstorm_turns"
    with Session(engine) as session:
        assert session.execute(
            text("SELECT count(*) FROM events WHERE workspace_id = :ws"), {"ws": WS}
        ).scalar_one() == events_before
        assert session.execute(
            text("SELECT count(*) FROM audit_events WHERE action = 'agent.limit_exceeded' "
                 "AND correlation_id = 'corr-verifier-limit'")
        ).scalar_one() == 1
