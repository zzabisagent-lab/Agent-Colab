"""V-P6-04 and V-P6-27: summary, decision and taskify (P6-09, development plan §7F).

The summarizer prefers an Agent that is not a participant; nothing reaches the channel before the
facilitator approves; a Decision creates one Task per action item with mandatory acceptance
criteria and provenance that resolves in both directions.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application import brainstorm as bs
from server.artifacts.storage import ArtifactStorage
from server.brainstorm import taskify as tsk
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from tests.integration.brainstorm_seed import AGENT_NAMES, OUTSIDER, T0, Seed, event_types

pytestmark = pytest.mark.db
SEED = Seed("bsd")
ITEMS = (
    {
        "statement": "draft the pricing model",
        "criteria": [{"statement": "model reviewed", "check_type": "evidence", "required": True}],
    },
    {"statement": "book the customer interviews", "criteria": ["five interviews scheduled"]},
)


@pytest.fixture(scope="module")
def engine(database_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    eng_ = make_engine(database_url)
    SEED.artifact_storage = ArtifactStorage(tmp_path_factory.mktemp("bs-artifacts"))
    SEED.create(eng_)
    yield eng_
    eng_.dispose()


def _open_session(engine: Engine, clock: FixedClock, key: str) -> str:
    participants = (*tuple(SEED.agent_account(n) for n in AGENT_NAMES), SEED.human)
    result = SEED.run(
        engine,
        bs.StartBrainstorm(channel_id=SEED.channel_id, topic="Pricing", participants=participants),
        SEED.facilitator,
        key,
        clock,
    )
    bid = result.resource_id
    SEED.run(
        engine,
        bs.ContributeTurn(brainstorm_id=bid, body="tiered pricing", contribution_type="IDEA"),
        SEED.agent_account(AGENT_NAMES[0]),
        f"{key}-c1",
        clock,
    )
    SEED.run(
        engine,
        bs.ContributeTurn(brainstorm_id=bid, body="usage risk", contribution_type="CHALLENGE"),
        SEED.agent_account(AGENT_NAMES[1]),
        f"{key}-c2",
        clock,
    )
    return bid


def _posts(engine: Engine, brainstorm_id: str) -> list[str]:
    with Session(engine) as s:
        return [
            str(r[0])
            for r in s.execute(
                text(
                    "SELECT dedupe_key FROM delivery_outbox WHERE kind = 'mattermost.post' "
                    "AND dedupe_key LIKE 'bs-summary:%' AND payload->>'message' LIKE :topic"
                ),
                {"topic": f"%{brainstorm_id}%"},
            ).all()
        ]


def test_summary_prefers_a_non_participant_and_posts_only_after_approval(engine: Engine) -> None:
    clock = FixedClock(T0)
    bid = _open_session(engine, clock, "s1")

    result = SEED.run(
        engine, bs.SummarizeBrainstorm(brainstorm_id=bid), SEED.facilitator, "s2", clock
    )
    # V-P6-27: the outsider Agent is chosen over the three participants
    assert result.data["summarizer_account_id"] == SEED.agent_account(OUTSIDER)
    assert result.data["summarizer_is_participant"] is False
    assert result.data["status"] == "DRAFT" and result.data["posted"] is False
    assert result.data["artifact_id"]
    assert "SUMMARY_RECORDED" in event_types(engine, bid)
    assert _posts(engine, bid) == []  # nothing posted before approval

    # the draft carries the transcript's arguments and challenges (source for V-P6-08)
    view = SEED.read(engine, bs.brainstorm_view, bid)
    summary_id = view["summaries"][0]["summary_id"]
    with Session(engine) as s:
        body = s.execute(
            text("SELECT body FROM brainstorm_summaries WHERE summary_id = :s"), {"s": summary_id}
        ).scalar_one()
    assert "tiered pricing" in body and "usage risk" in body
    assert "Ideas and arguments" in body and "Challenges and alternatives" in body

    # only the facilitator approves, and approval is what posts it
    assert (
        SEED.run_expect(engine, bs.ApproveSummary(summary_id=summary_id), SEED.human, "s3", clock)
        == "BRAINSTORM_FACILITATOR_ONLY"
    )
    assert _posts(engine, bid) == []
    approved = SEED.run(
        engine, bs.ApproveSummary(summary_id=summary_id), SEED.facilitator, "s4", clock
    )
    assert approved.data == {"summary_id": summary_id, "status": "APPROVED", "posted": True}
    assert _posts(engine, bid) == [f"bs-summary:{summary_id}"]
    assert (
        SEED.run_expect(
            engine, bs.ApproveSummary(summary_id=summary_id), SEED.facilitator, "s5", clock
        )
        == "SUMMARY_NOT_DRAFT"
    )


def test_summary_falls_back_to_a_participant_when_no_outsider_is_available(
    engine: Engine,
) -> None:
    clock = FixedClock(T0)
    participants = tuple(SEED.agent_account(n) for n in (*AGENT_NAMES, OUTSIDER))
    bid = SEED.run(
        engine,
        bs.StartBrainstorm(
            channel_id=SEED.channel_id, topic="Everyone in", participants=participants
        ),
        SEED.facilitator,
        "fb1",
        clock,
    ).resource_id
    result = SEED.run(
        engine, bs.SummarizeBrainstorm(brainstorm_id=bid), SEED.facilitator, "fb2", clock
    )
    assert result.data["summarizer_is_participant"] is True
    assert result.data["summarizer_account_id"] in {SEED.agent_account(n) for n in AGENT_NAMES} | {
        SEED.agent_account(OUTSIDER)
    }


def test_decision_and_taskify_carry_provenance_both_ways(engine: Engine) -> None:
    clock = FixedClock(T0)
    bid = _open_session(engine, clock, "d1")
    transcript = SEED.read(engine, bs.transcript_view, bid)
    sources = tuple(str(t["event_id"]) for t in transcript)

    # only the facilitator decides (§7F)
    assert (
        SEED.run_expect(
            engine,
            bs.RecordDecision(
                brainstorm_id=bid, statement="adopt tiered pricing", rationale="lower risk"
            ),
            SEED.agent_account(AGENT_NAMES[0]),
            "d2",
            clock,
        )
        == "BRAINSTORM_FACILITATOR_ONLY"
    )

    # an action item without acceptance criteria is refused (§7D.1 is mandatory)
    assert (
        SEED.run_expect(
            engine,
            bs.RecordDecision(
                brainstorm_id=bid,
                statement="adopt tiered pricing",
                rationale="lower risk",
                action_items=({"statement": "do the thing"},),
            ),
            SEED.facilitator,
            "d3",
            clock,
        )
        == "DECISION_ACTION_ITEM_CRITERIA_REQUIRED"
    )

    decision = SEED.run(
        engine,
        bs.RecordDecision(
            brainstorm_id=bid,
            statement="adopt tiered pricing",
            rationale="lower risk, clearer story",
            source_event_ids=sources,
            action_items=ITEMS,
            vote={"up": 2, "down": 1, "voters": [SEED.human]},
        ),
        SEED.facilitator,
        "d4",
        clock,
    )
    decision_id = decision.resource_id
    assert decision.data["action_items"] == 2
    assert "DECISION_RECORDED" in event_types(engine, decision_id)

    view = SEED.read(engine, bs.decision_view, decision_id)
    assert view["statement"] == "adopt tiered pricing"
    assert list(view["source_event_ids"]) == list(sources)
    assert view["vote"]["up"] == 2 and view["status"] == "recorded"

    result = SEED.run(
        engine, bs.TaskifyDecision(decision_id=decision_id), SEED.facilitator, "d5", clock
    )
    tasks = [t["task_id"] for t in result.data["tasks"]]
    assert len(tasks) == 2

    with Session(engine) as s:
        # Decision -> Task
        linked = s.execute(
            text(
                "SELECT task_id, action_item, item_index FROM decision_tasks "
                "WHERE decision_id = :d ORDER BY item_index"
            ),
            {"d": decision_id},
        ).all()
        assert [str(r[0]) for r in linked] == tasks
        assert [str(r[1]) for r in linked] == [i["statement"] for i in ITEMS]
        # every Task exists with its mandatory acceptance criteria
        for task_id, item in zip(tasks, ITEMS, strict=True):
            title = s.execute(
                text("SELECT title FROM tasks_projection WHERE task_id = :t"), {"t": task_id}
            ).scalar_one()
            assert title == item["statement"]
            criteria = s.execute(
                text("SELECT count(*) FROM task_acceptance_criteria WHERE task_id = :t"),
                {"t": task_id},
            ).scalar_one()
            assert criteria >= 1
        # Task -> Decision
        provenance = tsk.provenance(s, tasks[0])
        assert provenance is not None
        assert provenance["decision_id"] == decision_id
        assert provenance["brainstorm_id"] == bid
        status = s.execute(
            text("SELECT status FROM brainstorm_decisions WHERE decision_id = :d"),
            {"d": decision_id},
        ).scalar_one()
        assert status == "taskified"

    # taskify is idempotent: the same Tasks come back, no duplicates are created
    again = SEED.run(
        engine, bs.TaskifyDecision(decision_id=decision_id), SEED.facilitator, "d6", clock
    )
    assert [t["task_id"] for t in again.data["tasks"]] == tasks
    assert all(t["replayed"] for t in again.data["tasks"])
    with Session(engine) as s:
        assert (
            s.execute(
                text("SELECT count(*) FROM decision_tasks WHERE decision_id = :d"),
                {"d": decision_id},
            ).scalar_one()
            == 2
        )


def test_taskify_without_action_items_is_refused(engine: Engine) -> None:
    clock = FixedClock(T0)
    bid = _open_session(engine, clock, "na1")
    decision = SEED.run(
        engine,
        bs.RecordDecision(brainstorm_id=bid, statement="park the question", rationale="needs data"),
        SEED.facilitator,
        "na2",
        clock,
    )
    assert (
        SEED.run_expect(
            engine,
            bs.TaskifyDecision(decision_id=decision.resource_id),
            SEED.facilitator,
            "na3",
            clock,
        )
        == "DECISION_HAS_NO_ACTION_ITEMS"
    )
