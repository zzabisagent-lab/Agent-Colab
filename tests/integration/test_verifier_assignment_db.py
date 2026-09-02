"""V-P3-14 (implementer/ineligible Agents excluded from Verifier selection) and V-P3-24 (3
candidates: 2 eligible + 1 ineligible; best-scored eligible chosen with criteria/evidence delivered;
first candidate silent for 10 minutes → reassigned; none left → WAITING + Administrator
notification Event)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

import server.application.documents  # noqa: F401
from server.application import tasks as tk
from server.application.authz import AllowAllAuthorizer
from server.application.bus import CommandError
from server.application.verification import CreateVerificationRun
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.domain.criteria import criteria_id
from server.events.postgres_store import PostgresEventStore
from server.verification import assignment
from server.verification.independence import Identity
from tests.integration.phase3_seed import CRITERIA, Seed, event_types, status_of

pytestmark = pytest.mark.db
SEED = Seed("va")
T0 = dt.datetime(2026, 5, 4, 9, 0, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    SEED.create(eng)
    with eng.begin() as c:
        SEED.add_agent(c, "acct-va-impl", capacity=5)  # the implementer
        # eligible: domain capability, online, capacity; v1 idle, v2 loaded later
        SEED.add_agent(c, "acct-va-v1", capacity=5)
        SEED.add_agent(c, "acct-va-v2", capacity=5)
        # ineligible for one reason each
        SEED.add_agent(c, "acct-va-alias", capacity=5)  # alias of the implementer
        SEED.add_agent(c, "acct-va-shared", capacity=5)  # shared credential
        SEED.add_agent(c, "acct-va-offline", capacity=5, online=False)
        SEED.add_agent(
            c, "acct-va-ops", capacity=5, capabilities=(("cap-ops", "ops"),)
        )  # wrong domain
        SEED.add_account(c, "acct-va-reviewer", "human")
        c.execute(
            text(
                "INSERT INTO account_aliases (account_id, alias_of_account_id, reason) "
                "VALUES (:a, :b, 'same operator')"
            ),
            {"a": SEED.account("acct-va-alias"), "b": SEED.account("acct-va-impl")},
        )
    yield eng
    eng.dispose()


class AgentsOnlyPolicy(AllowAllAuthorizer):
    """Policy double: Humans of this seed hold no ``verification.submit`` permission."""

    def require(
        self, session: Any, principal_account_id: str, permission: str, **scope: Any
    ) -> None:
        if permission == "verification.submit" and principal_account_id in (
            "acct-va-human",
            "acct-va-reviewer",
        ):
            raise CommandError("POLICY_DENIED", f"{permission} denied", status=403)


def _run(engine: Engine, cmd: Any, who: str, key: str, clock: FixedClock) -> Any:
    return SEED.run(engine, cmd, SEED.principal(who), key, clock, authorizer=AgentsOnlyPolicy())


def _implemented_task(engine: Engine, key: str, clock: FixedClock, risk: str = "LOW") -> str:
    tid = str(
        _run(
            engine,
            tk.CreateTask("verify me", str(SEED.channel), "research", risk, criteria=CRITERIA),
            "acct-va-human",
            f"{key}-create",
            clock,
        ).resource_id
    )
    _run(engine, tk.DelegateTask(tid, "acct-va-impl"), "acct-va-human", f"{key}-d", clock)
    _run(engine, tk.AcceptTask(tid), "acct-va-impl", f"{key}-a", clock)
    _run(engine, tk.StartTask(tid), "acct-va-impl", f"{key}-s", clock)
    evidence = (f"{criteria_id(tid, 1, 0, 'evidence attached')}:art-1",)
    _run(engine, tk.SubmitImplementation(tid, evidence, 1), "acct-va-impl", f"{key}-i", clock)
    return tid


def _auto_run(task_id: str) -> CreateVerificationRun:
    return CreateVerificationRun(
        target_type="task",
        target_id=task_id,
        implementer_account_id="acct-va-impl",
        verifier_account_id="",
        implementer_credential_fingerprint="fp-acct-va-shared",  # credential shared with va-shared
        verifier_credential_fingerprint="",
        target_commit="abc123",
        effective_policy_hash="0" * 64,
        implementer_agent_id=SEED.agents["acct-va-impl"],
        task_id=task_id,
        auto_assign=True,
    )


def _offers(engine: Engine, task_id: str) -> list[tuple[int, str, str, str | None]]:
    with Session(engine) as s:
        return [
            (int(r[0]), str(r[1]), str(r[2]), r[3])
            for r in s.execute(
                text(
                    "SELECT va.candidate_rank, a.account_id, va.status, va.work_item_id "
                    "FROM verifier_assignments va JOIN accounts a ON a.id = va.account_id "
                    "WHERE va.task_id = :t ORDER BY va.candidate_rank"
                ),
                {"t": task_id},
            ).all()
        ]


def test_eligibility_excludes_implementer_alias_credential_offline_and_wrong_domain(
    engine: Engine,
) -> None:
    with Session(engine) as s:
        found = assignment.eligible_verifiers(
            s,
            workspace_id=str(SEED.ws),
            domain="research",
            risk="LOW",
            channel_id=SEED.channel_id,
            implementer=Identity(  # authenticated with the credential acct-va-shared also holds
                str(SEED.account("acct-va-impl")), "fp-acct-va-shared", SEED.agents["acct-va-impl"]
            ),
            authorizer=AllowAllAuthorizer(),
        )
    # Agents with the domain capability score 3; Humans (no domain match) score 1
    assert [c.account_id for c in found] == [
        "acct-va-v1",
        "acct-va-v2",
        "acct-va-human",
        "acct-va-reviewer",
    ]
    assert found[0].score == 3 and found[0].domain_match and found[0].load == 0
    assert found[2].score == 1 and found[2].human
    # HIGH risk requires a Human Verifier: Agents drop out, the Human reviewer is preferred
    with Session(engine) as s:
        humans = assignment.eligible_verifiers(
            s,
            workspace_id=str(SEED.ws),
            domain="research",
            risk="HIGH",
            channel_id=SEED.channel_id,
            implementer=Identity(str(SEED.account("acct-va-impl")), "fp-acct-va-impl", None),
            authorizer=AllowAllAuthorizer(),
        )
    assert [c.account_id for c in humans] == ["acct-va-human", "acct-va-reviewer"]
    assert all(c.human and c.score >= 2 for c in humans)


def test_best_scored_candidate_gets_the_assignment_with_criteria_and_evidence(
    engine: Engine,
) -> None:
    clock = FixedClock(T0)
    tid = _implemented_task(engine, "best", clock)
    res = _run(engine, _auto_run(tid), "acct-va-human", "best-run", clock)
    vid = str(res.resource_id)
    assert _offers(engine, tid)[0][:3] == (1, "acct-va-v1", "offered")
    with Session(engine) as s:
        run_row = s.execute(
            text(
                "SELECT a.account_id FROM verification_runs v JOIN accounts "
                "a ON a.id = v.verifier_account_id WHERE v.verification_id = :v"
            ),
            {"v": vid},
        ).scalar_one()
        item = s.execute(
            text(
                "SELECT agent_id, kind, payload FROM work_items WHERE "
                "task_id = :t AND kind = 'verification_assignment'"
            ),
            {"t": tid},
        ).first()
    assert run_row == "acct-va-v1"
    assert item is not None and item[0] == SEED.agents["acct-va-v1"]
    payload = item[2]
    assert payload["verification_id"] == vid and payload["target_commit"] == "abc123"
    assert [c["statement"] for c in payload["criteria"]] == ["evidence attached"]
    assert payload["evidence_manifest"] == [f"{criteria_id(tid, 1, 0, 'evidence attached')}:art-1"]
    assert payload["read_only_access"]["task"] == f"colab://task/{tid}"
    assert "VERIFIER_ASSIGNED" in event_types(engine, vid)
    # the offer is not a Task transition and never repeats on an idempotent retry
    again = _run(engine, _auto_run(tid), "acct-va-human", "best-run", clock)
    assert again.resource_id == vid and len(_offers(engine, tid)) == 1


def test_silent_candidate_is_replaced_after_ten_minutes_then_exhausted(engine: Engine) -> None:
    clock = FixedClock(T0 + dt.timedelta(hours=1))
    tid = _implemented_task(engine, "silent", clock)
    first = str(_run(engine, _auto_run(tid), "acct-va-human", "silent-run", clock).resource_id)
    assert [o[1] for o in _offers(engine, tid)] == ["acct-va-v1"]

    def sweep() -> list[assignment.TimeoutOutcome]:
        """Workspace-wide sweep, reported for this Task only (other tests' offers age too)."""
        with Session(engine) as s, s.begin():
            out = assignment.sweep_timeouts(
                s,
                PostgresEventStore(s, clock=clock),
                clock=clock,
                workspace_id=str(SEED.ws),
                actor=SEED.principal("acct-va-system"),
                authorizer=AgentsOnlyPolicy(),
            )
        return [o for o in out if o.task_id == tid]

    clock.advance(dt.timedelta(minutes=9, seconds=59))
    assert sweep() == []
    clock.advance(dt.timedelta(seconds=2))
    out = sweep()
    assert [(o.task_id, o.code) for o in out] == [(tid, "REASSIGNED")]
    offers = _offers(engine, tid)
    assert [(o[0], o[1], o[2]) for o in offers] == [
        (1, "acct-va-v1", "timed_out"),
        (2, "acct-va-v2", "offered"),
    ]
    with Session(engine) as s:
        statuses = dict(
            s.execute(
                text("SELECT verification_id, status FROM verification_runs WHERE task_id = :t"),
                {"t": tid},
            ).all()
        )
        v1_item = s.execute(
            text("SELECT status FROM work_items WHERE work_item_id = :w"), {"w": offers[0][3]}
        ).scalar_one()
    assert statuses[first] == "CANCELLED" and v1_item == "CANCELLED"
    second = out[0].next_verification_id
    assert second is not None and statuses[second] == "PLANNED"
    # v2 is silent as well and nobody else is eligible → EXHAUSTED, WAITING, Administrators told
    clock.advance(dt.timedelta(minutes=10, seconds=1))
    out = sweep()
    assert [(o.task_id, o.code) for o in out] == [(tid, "EXHAUSTED")]
    assert status_of(engine, tid) == "WAITING"
    assert event_types(engine, tid)[-1] == "TASK_WAITING"
    assert "VERIFIER_ASSIGNMENT_EXHAUSTED" in event_types(engine, second)
    assert [o[2] for o in _offers(engine, tid)] == ["timed_out", "exhausted"]
    with Session(engine) as s:
        payload = s.execute(
            text(
                "SELECT payload FROM events WHERE type = 'VERIFIER_ASSIGNMENT_EXHAUSTED' "
                "AND aggregate_id = :v"
            ),
            {"v": second},
        ).scalar_one()
    assert payload["task_id"] == tid and payload["offers"] == 2


def test_no_eligible_verifier_is_a_stable_error_with_zero_side_effects(engine: Engine) -> None:
    clock = FixedClock(T0 + dt.timedelta(hours=2))
    tid = _implemented_task(engine, "none", clock, risk="LOW")
    with Session(engine) as s, s.begin():
        s.execute(
            text("UPDATE agents SET online = false WHERE agent_id IN (:a, :b)"),
            {"a": SEED.agents["acct-va-v1"], "b": SEED.agents["acct-va-v2"]},
        )
    try:
        with pytest.raises(CommandError) as exc:
            _run(engine, _auto_run(tid), "acct-va-human", "none-run", clock)
        assert exc.value.code == "VERIFIER_NONE_ELIGIBLE"
    finally:
        with Session(engine) as s, s.begin():
            s.execute(
                text("UPDATE agents SET online = true WHERE agent_id IN (:a, :b)"),
                {"a": SEED.agents["acct-va-v1"], "b": SEED.agents["acct-va-v2"]},
            )
    with Session(engine) as s:
        runs = s.execute(
            text("SELECT count(*) FROM verification_runs WHERE task_id = :t"), {"t": tid}
        ).scalar_one()
    assert runs == 0 and _offers(engine, tid) == []
