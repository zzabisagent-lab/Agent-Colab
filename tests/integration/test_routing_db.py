"""V-P3-03 (capability routing: only the eligible intersection, ties by ascending agent_id) and
V-P3-10 (a non-member Agent is denied channel access by policy and never routed)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.agents import routing
from server.application.authz import BusAuthorizer
from server.application.bus import CommandError
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.policy.authorization import Authorizer
from server.policy.repository import PostgresPolicyRepository
from tests.integration.phase3_seed import Seed

pytestmark = pytest.mark.db
SEED = Seed("route")
CLOCK = FixedClock(dt.datetime(2026, 5, 1, tzinfo=dt.UTC))


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    SEED.create(eng)
    with eng.begin() as c:
        # eligible pair with identical scores → tie broken by agent_id
        SEED.add_agent(c, "acct-route-b")
        SEED.add_agent(c, "acct-route-a")
        # each of the following fails exactly one eligibility condition
        SEED.add_agent(c, "acct-route-inactive", status="suspended")
        SEED.add_agent(c, "acct-route-offline", online=False)
        SEED.add_agent(c, "acct-route-nonmember", member=False)
        SEED.add_agent(c, "acct-route-nocap", capabilities=(("cap-ops", "ops"),))
        SEED.add_agent(c, "acct-route-full", capacity=1)
        SEED.add_agent(c, "acct-route-denied")
        SEED.add_agent(c, "acct-route-bot", adapter_type="mattermost_bot")
        # a lower-scored but eligible Agent: capacity left, but already loaded (no +1)
        SEED.add_agent(c, "acct-route-generic", capacity=2)
    with Session(eng) as s, s.begin():
        # the "full" Agent already carries one active Task (capacity 1 → no room)
        s.execute(
            text(
                "INSERT INTO tasks_projection (task_id, workspace_id, root_task_id, channel_id, "
                "title, domain, risk, status, assignee_account_id, created_at, updated_at) VALUES "
                "('task-route-busy', :w, 'task-route-busy', :c, 'busy', 'research', 'LOW', "
                "'RUNNING', :a, :n, :n)"
            ),
            {
                "w": SEED.ws,
                "c": SEED.channel,
                "a": SEED.account("acct-route-full"),
                "n": CLOCK.now(),
            },
        )
        s.execute(
            text(
                "INSERT INTO tasks_projection (task_id, workspace_id, root_task_id, channel_id, "
                "title, domain, risk, status, assignee_account_id, created_at, updated_at) VALUES "
                "('task-route-loaded', :w, 'task-route-loaded', :c, 'loaded', 'research', 'LOW', "
                "'ACCEPTED', :a, :n, :n)"
            ),
            {
                "w": SEED.ws,
                "c": SEED.channel,
                "a": SEED.account("acct-route-generic"),
                "n": CLOCK.now(),
            },
        )
        repo = PostgresPolicyRepository()
        repo.create_role(s, SEED.ws, "route-worker", "worker")
        repo.commit_role_version(
            s, "route-worker", ["task.*", "work.poll"], [], {}, SEED.account("acct-route-human")
        )
        repo.create_role(s, SEED.ws, "route-denied", "explicit deny")
        repo.commit_role_version(
            s, "route-denied", ["task.*"], ["task.accept"], {}, SEED.account("acct-route-human")
        )
        for name in SEED.agents:
            role = "route-denied" if name == "acct-route-denied" else "route-worker"
            repo.assign_role(
                s, SEED.account(name), role, SEED.account("acct-route-human"), CLOCK.now()
            )
    yield eng
    eng.dispose()


def _authorizer() -> BusAuthorizer:
    return BusAuthorizer(Authorizer(clock=CLOCK))


def test_only_the_eligible_intersection_is_selected_and_ties_are_deterministic(
    engine: Engine,
) -> None:
    with Session(engine) as s, s.begin():
        found = routing.candidates(
            s,
            workspace_id=str(SEED.ws),
            channel_uuid=str(SEED.channel),
            required_capability="cap-research",
            domain="research",
            needs_secret_handles=True,
            authorizer=_authorizer(),
            correlation_id="corr-route",
        )
        assert [c.agent_id for c in found] == [
            "agent-route-a",
            "agent-route-b",
            "agent-route-generic",
        ]
        assert found[0].score == found[1].score == 3 and found[2].score == 2
        # a second evaluation reproduces the same order (deterministic, ascending agent_id on ties)
        again = routing.candidates(
            s,
            workspace_id=str(SEED.ws),
            channel_uuid=str(SEED.channel),
            required_capability="cap-research",
            domain="research",
            needs_secret_handles=True,
            authorizer=_authorizer(),
        )
        assert [c.agent_id for c in again] == [c.agent_id for c in found]
        # without the secret-handle requirement the bot adapter joins the eligible set
        with_bot = routing.candidates(
            s,
            workspace_id=str(SEED.ws),
            channel_uuid=str(SEED.channel),
            required_capability="cap-research",
            domain="research",
            authorizer=_authorizer(),
        )
        assert "agent-route-bot" in [c.agent_id for c in with_bot]
        chosen = routing.select_assignee(
            s,
            workspace_id=str(SEED.ws),
            task_id="task-route-1",
            channel_uuid=str(SEED.channel),
            required_capability="cap-research",
            domain="research",
            correlation_id="corr-route",
            actor_label="acct-route-human",
            authorizer=_authorizer(),
            clock=CLOCK,
        )
        assert chosen is not None and chosen.agent_id == "agent-route-a"
        decisions = routing.decisions_for(s, "task-route-1")
        assert len(decisions) == 1 and decisions[0]["selected_agent_id"] == "agent-route-a"
        assert [c["agent_id"] for c in decisions[0]["candidates"]][:2] == [
            "agent-route-a",
            "agent-route-b",
        ]
        audit = s.execute(
            text(
                "SELECT count(*) FROM audit_events WHERE action = 'routing.select' "
                "AND target_id = 'task-route-1' AND workspace_id = :w"
            ),
            {"w": SEED.ws},
        ).scalar_one()
        assert audit == 1


def test_no_candidate_is_recorded(engine: Engine) -> None:
    with Session(engine) as s, s.begin():
        chosen = routing.select_assignee(
            s,
            workspace_id=str(SEED.ws),
            task_id="task-route-none",
            channel_uuid=str(SEED.channel),
            required_capability="cap-missing",
            domain="research",
            correlation_id="corr-route",
            actor_label="acct-route-human",
            authorizer=_authorizer(),
            clock=CLOCK,
        )
        assert chosen is None
        rows = routing.decisions_for(s, "task-route-none")
        assert rows[0]["reason_code"] == "NO_CANDIDATE" and rows[0]["candidates"] == []


def test_non_member_agent_is_denied_channel_access(engine: Engine) -> None:
    """V-P3-10: read/write by a non-member Agent is denied by policy (and never routed)."""
    auth = _authorizer()
    with Session(engine) as s, s.begin():
        for permission in ("task.read", "task.progress"):
            with pytest.raises(CommandError) as exc:
                auth.require(
                    s,
                    "acct-route-nonmember",
                    permission,
                    channel_id=str(SEED.channel),
                    domain="research",
                    correlation_id="corr-route",
                )
            assert exc.value.status == 403
        auth.require(
            s, "acct-route-a", "task.read", channel_id=str(SEED.channel), domain="research"
        )
        found = routing.candidates(
            s,
            workspace_id=str(SEED.ws),
            channel_uuid=str(SEED.channel),
            required_capability=None,
            domain="research",
            authorizer=auth,
        )
        assert "agent-route-nonmember" not in [c.agent_id for c in found]
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE action = 'policy.deny' "
                    "AND actor_label = 'acct-route-nonmember'"
                )
            ).scalar_one()
            >= 2
        )
