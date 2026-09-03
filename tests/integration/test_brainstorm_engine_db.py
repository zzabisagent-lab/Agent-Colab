"""V-P6-03 and V-P6-26: the Brainstorm turn engine (P6-02, development plan §7F).

Three Agents take turns round-robin; a consecutive same-Agent utterance is rejected and pauses the
session with a facilitator guidance request; turn, budget and time overruns pause the same way;
the facilitator's resume continues the same order.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application import brainstorm as bs
from server.brainstorm import engine as eng
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from tests.integration.brainstorm_seed import (
    AGENT_NAMES,
    T0,
    Seed,
    event_types,
    guidance_requests,
    work_items,
)

pytestmark = pytest.mark.db
SEED = Seed("bse")


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng_ = make_engine(database_url)
    SEED.create(eng_)
    yield eng_
    eng_.dispose()


def _start(engine: Engine, clock: FixedClock, limits: dict[str, int], key: str) -> str:
    participants = (*tuple(SEED.agent_account(n) for n in AGENT_NAMES), SEED.human)
    result = SEED.run(
        engine,
        bs.StartBrainstorm(
            channel_id=SEED.channel_id,
            topic="Q4 roadmap",
            participants=participants,
            limits=limits,
        ),
        SEED.facilitator,
        key,
        clock,
    )
    return result.resource_id


def _status(engine: Engine, brainstorm_id: str) -> tuple[str, str | None]:
    with Session(engine) as s:
        row = s.execute(
            text("SELECT status, paused_reason FROM brainstorms WHERE brainstorm_id = :b"),
            {"b": brainstorm_id},
        ).first()
    assert row is not None
    return str(row[0]), row[1]


def test_round_robin_order_consecutive_rejection_pause_and_resume(engine: Engine) -> None:
    clock = FixedClock(T0)
    bid = _start(
        engine, clock, {"turns_per_agent": 3, "max_consecutive": 1, "total_turns": 9}, "k1"
    )

    # the first Agent seat holds turn 1 and has a brainstorm_turn work item waiting
    view = SEED.read(engine, bs.brainstorm_view, bid)
    assert view["status"] == "OPEN"
    assert view["next_agent_account_id"] == SEED.agent_account(AGENT_NAMES[0])
    queued = work_items(engine, bid)
    assert [w["agent_id"] for w in queued] == [SEED.agent_ids[AGENT_NAMES[0]]]
    assert queued[0]["kind"] == "brainstorm_turn"

    # alpha contributes; the cursor moves to beta (order reproducible)
    SEED.run(
        engine,
        bs.ContributeTurn(brainstorm_id=bid, body="ship the beta first", contribution_type="IDEA"),
        SEED.agent_account(AGENT_NAMES[0]),
        "k2",
        clock,
    )
    view = SEED.read(engine, bs.brainstorm_view, bid)
    assert view["turn_no"] == 1
    assert view["next_agent_account_id"] == SEED.agent_account(AGENT_NAMES[1])

    # alpha again, immediately: rejected, session PAUSED, facilitator guidance requested (V-P6-26)
    code = SEED.run_expect(
        engine,
        bs.ContributeTurn(brainstorm_id=bid, body="and one more", contribution_type="IDEA"),
        SEED.agent_account(AGENT_NAMES[0]),
        "k3",
        clock,
    )
    assert code == "MAX_CONSECUTIVE_EXCEEDED"
    assert _status(engine, bid) == ("PAUSED", "MAX_CONSECUTIVE_EXCEEDED")
    guidance = guidance_requests(engine, bid)
    assert len(guidance) == 1
    assert guidance[0]["payload"]["reason"] == "BRAINSTORM_GUIDANCE_REQUESTED"
    assert guidance[0]["payload"]["reason_code"] == "MAX_CONSECUTIVE_EXCEEDED"
    view = SEED.read(engine, bs.brainstorm_view, bid)
    assert view["turn_no"] == 1  # the rejected utterance was not recorded

    # while paused nothing may be contributed
    assert (
        SEED.run_expect(
            engine,
            bs.ContributeTurn(brainstorm_id=bid, body="hello", contribution_type="IDEA"),
            SEED.agent_account(AGENT_NAMES[1]),
            "k4",
            clock,
        )
        == "BRAINSTORM_NOT_OPEN"
    )

    # the facilitator resumes and the order continues where it stopped
    SEED.run(engine, bs.ResumeBrainstorm(brainstorm_id=bid), SEED.facilitator, "k5", clock)
    assert _status(engine, bid) == ("OPEN", None)
    view = SEED.read(engine, bs.brainstorm_view, bid)
    assert view["next_agent_account_id"] == SEED.agent_account(AGENT_NAMES[1])

    for index, name in enumerate(AGENT_NAMES[1:], start=1):
        SEED.run(
            engine,
            bs.ContributeTurn(
                brainstorm_id=bid, body=f"point {index}", contribution_type="CHALLENGE"
            ),
            SEED.agent_account(name),
            f"k6-{index}",
            clock,
        )
    view = SEED.read(engine, bs.brainstorm_view, bid)
    assert view["turn_no"] == 3
    assert view["next_agent_account_id"] == SEED.agent_account(AGENT_NAMES[0])  # wrapped around

    # a Human speaks freely: no type needed, recorded as IDEA (§7F)
    SEED.run(
        engine,
        bs.ContributeTurn(brainstorm_id=bid, body="what about pricing?"),
        SEED.human,
        "k7",
        clock,
    )
    transcript = SEED.read(engine, bs.transcript_view, bid)
    assert [t["contribution_type"] for t in transcript] == [
        "IDEA",
        "CHALLENGE",
        "CHALLENGE",
        "IDEA",
    ]
    assert transcript[-1]["account_id"] == SEED.human
    assert event_types(engine, bid) == [
        "BRAINSTORM_OPENED",
        "IDEA_RECORDED",
        "BRAINSTORM_PAUSED",
        "BRAINSTORM_RESUMED",
        "IDEA_RECORDED",
        "IDEA_RECORDED",
        "IDEA_RECORDED",
    ]

    # an Agent that is not on turn is refused without pausing the session
    assert (
        SEED.run_expect(
            engine,
            bs.ContributeTurn(brainstorm_id=bid, body="jumping in", contribution_type="IDEA"),
            SEED.agent_account(AGENT_NAMES[2]),
            "k8",
            clock,
        )
        == "BRAINSTORM_NOT_YOUR_TURN"
    )
    assert _status(engine, bid)[0] == "OPEN"


def test_turn_budget_and_time_limits_each_pause_with_guidance(engine: Engine) -> None:
    """V-P6-03: every limit kind pauses the session and asks the facilitator for guidance."""
    clock = FixedClock(T0)
    alpha = SEED.agent_account(AGENT_NAMES[0])

    # total turns
    bid = _start(engine, clock, {"total_turns": 1, "max_consecutive": 3}, "t1")
    SEED.run(
        engine,
        bs.ContributeTurn(brainstorm_id=bid, body="only one", contribution_type="IDEA"),
        alpha,
        "t2",
        clock,
    )
    assert (
        SEED.run_expect(
            engine,
            bs.ContributeTurn(brainstorm_id=bid, body="second", contribution_type="IDEA"),
            SEED.agent_account(AGENT_NAMES[1]),
            "t3",
            clock,
        )
        == "TOTAL_TURNS_EXCEEDED"
    )
    assert _status(engine, bid) == ("PAUSED", "TOTAL_TURNS_EXCEEDED")

    # per-Agent turns
    bid = _start(engine, clock, {"turns_per_agent": 1, "max_consecutive": 3}, "t4")
    SEED.run(
        engine,
        bs.ContributeTurn(brainstorm_id=bid, body="mine", contribution_type="IDEA"),
        alpha,
        "t5",
        clock,
    )
    for name in AGENT_NAMES[1:]:
        SEED.run(
            engine,
            bs.ContributeTurn(brainstorm_id=bid, body="theirs", contribution_type="IDEA"),
            SEED.agent_account(name),
            f"t6-{name}",
            clock,
        )
    assert (
        SEED.run_expect(
            engine,
            bs.ContributeTurn(brainstorm_id=bid, body="again", contribution_type="IDEA"),
            alpha,
            "t7",
            clock,
        )
        == "TURNS_PER_AGENT_EXCEEDED"
    )
    assert _status(engine, bid) == ("PAUSED", "TURNS_PER_AGENT_EXCEEDED")

    # budget
    bid = _start(engine, clock, {"budget_cost_units": 100}, "t8")
    SEED.spend(engine, bid, 101, T0)
    assert (
        SEED.run_expect(
            engine,
            bs.ContributeTurn(brainstorm_id=bid, body="costly", contribution_type="IDEA"),
            alpha,
            "t9",
            clock,
        )
        == "BUDGET_EXCEEDED"
    )
    assert _status(engine, bid) == ("PAUSED", "BUDGET_EXCEEDED")

    # time
    bid = _start(engine, clock, {"time_limit_minutes": 30}, "t10")
    late = FixedClock(T0 + dt.timedelta(minutes=31))
    assert (
        SEED.run_expect(
            engine,
            bs.ContributeTurn(brainstorm_id=bid, body="late", contribution_type="IDEA"),
            alpha,
            "t11",
            late,
        )
        == "TIME_LIMIT_EXCEEDED"
    )
    assert _status(engine, bid) == ("PAUSED", "TIME_LIMIT_EXCEEDED")
    assert len(guidance_requests(engine, bid)) == 1

    # resuming with adjusted limits lets the session continue (§7F)
    SEED.run(
        engine,
        bs.ResumeBrainstorm(brainstorm_id=bid, limits={"time_limit_minutes": 120}),
        SEED.facilitator,
        "t12",
        late,
    )
    SEED.run(
        engine,
        bs.ContributeTurn(brainstorm_id=bid, body="carrying on", contribution_type="IDEA"),
        alpha,
        "t13",
        late,
    )
    assert _status(engine, bid) == ("OPEN", None)
    assert SEED.read(engine, bs.brainstorm_view, bid)["turn_no"] == 1


def test_only_the_facilitator_pauses_resumes_and_closes(engine: Engine) -> None:
    clock = FixedClock(T0)
    bid = _start(engine, clock, {}, "f1")
    for command, key in (
        (bs.PauseBrainstorm(brainstorm_id=bid), "f2"),
        (bs.CloseBrainstorm(brainstorm_id=bid), "f3"),
    ):
        assert (
            SEED.run_expect(engine, command, SEED.human, key, clock)
            == "BRAINSTORM_FACILITATOR_ONLY"
        )
    SEED.run(engine, bs.PauseBrainstorm(brainstorm_id=bid), SEED.facilitator, "f4", clock)
    assert _status(engine, bid) == ("PAUSED", "FACILITATOR_PAUSE")
    SEED.run(engine, bs.ResumeBrainstorm(brainstorm_id=bid), SEED.facilitator, "f5", clock)
    SEED.run(engine, bs.CloseBrainstorm(brainstorm_id=bid), SEED.facilitator, "f6", clock)
    assert _status(engine, bid)[0] == "CLOSED"
    assert event_types(engine, bid)[-1] == "BRAINSTORM_CLOSED"
    # a closed session accepts nothing further
    assert (
        SEED.run_expect(
            engine,
            bs.ContributeTurn(brainstorm_id=bid, body="too late", contribution_type="IDEA"),
            SEED.agent_account(AGENT_NAMES[0]),
            "f7",
            clock,
        )
        == "BRAINSTORM_NOT_OPEN"
    )


def test_non_participants_cannot_contribute(engine: Engine) -> None:
    clock = FixedClock(T0)
    bid = _start(engine, clock, {}, "n1")
    assert (
        SEED.run_expect(
            engine,
            bs.ContributeTurn(brainstorm_id=bid, body="outsider", contribution_type="IDEA"),
            SEED.agent_account("delta"),
            "n2",
            clock,
        )
        == "BRAINSTORM_NOT_A_PARTICIPANT"
    )
    assert (
        eng.load(  # the session is untouched
            __import__("sqlalchemy.orm", fromlist=["Session"]).Session(engine), SEED.ws, bid
        )
        is not None
    )
