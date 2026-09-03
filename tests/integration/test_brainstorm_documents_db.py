"""V-P6-08 and the Brainstorm half of V-P6-20 through the production command path: closing a
session with `CloseBrainstorm` automatically drafts its document, that draft carries the session's
arguments, alternatives, decisions and limitations, and 20 closed sessions produce at least 19
drafts, each failure recording a stable reason code."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application import brainstorm as bs
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from tests.integration.brainstorm_seed import AGENT_NAMES, Seed

pytestmark = pytest.mark.db
SEED = Seed("bdoc")
CLOCK = FixedClock(dt.datetime(2026, 8, 3, 9, 0, tzinfo=dt.UTC))


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    SEED.create(eng)
    yield eng
    eng.dispose()


def _session(engine: Engine, key: str, *, with_content: bool = False) -> str:
    participants = (*tuple(SEED.agent_account(n) for n in AGENT_NAMES), SEED.human)
    bid = SEED.run(
        engine,
        bs.StartBrainstorm(
            channel_id=SEED.channel_id,
            topic=f"topic {key}",
            participants=participants,
            limits={"total_turns": 40, "turns_per_agent": 5, "max_consecutive": 1},
        ),
        SEED.facilitator,
        f"{key}-start",
        CLOCK,
    ).resource_id
    if with_content:
        for n, agent in enumerate(AGENT_NAMES):
            SEED.run(
                engine,
                bs.ContributeTurn(
                    brainstorm_id=bid,
                    body=f"option {n}: ship the smaller cut first",
                    contribution_type="IDEA" if n % 2 == 0 else "CHALLENGE",
                ),
                SEED.agent_account(agent),
                f"{key}-turn-{n}",
                CLOCK,
            )
        SEED.run(
            engine,
            bs.RecordDecision(
                brainstorm_id=bid,
                statement="ship the smaller cut in Q4",
                rationale="the larger cut misses the window",
                source_event_ids=[],
                action_items=[
                    {
                        "statement": "write the release note",
                        "criteria": [
                            {
                                "statement": "release note published",
                                "check_type": "evidence",
                                "required": True,
                            }
                        ],
                    }
                ],
            ),
            SEED.facilitator,
            f"{key}-decide",
            CLOCK,
        )
    SEED.run(engine, bs.CloseBrainstorm(brainstorm_id=bid), SEED.facilitator, f"{key}-close", CLOCK)
    return str(bid)


def _document(engine: Engine, brainstorm_id: str) -> dict[str, object] | None:
    with Session(engine) as s:
        row = (
            s.execute(
                text(
                    "SELECT v.document_id, v.status, v.storage_uri FROM document_versions v "
                    "JOIN documents d ON d.document_id = v.document_id "
                    "WHERE d.source_type = 'brainstorm' AND d.source_id = :b "
                    "ORDER BY v.version DESC LIMIT 1"
                ),
                {"b": brainstorm_id},
            )
            .mappings()
            .first()
        )
    return None if row is None else dict(row)


def test_closing_a_brainstorm_drafts_its_document(engine: Engine) -> None:
    """V-P6-08: the close command itself produces the draft, with the session's content."""
    brainstorm_id = _session(engine, f"doc-{uuid.uuid4().hex[:6]}", with_content=True)
    document = _document(engine, brainstorm_id)
    assert document is not None, "closing a Brainstorm must draft its document"
    from server.documents.store import DocumentStore

    workspace, doc_id, version = (
        str(document["storage_uri"]).removeprefix("colab-doc://").split("/")
    )
    path = DocumentStore().root / workspace / doc_id / f"{version}.md"
    body = path.read_text(encoding="utf-8")
    assert "ship the smaller cut" in body  # arguments and alternatives from the transcript
    assert "Decision" in body or "decision" in body
    for heading in ("Discussion", "Shortcomings", "Results"):
        assert heading in body, heading


def test_twenty_closed_brainstorms_draft_at_least_nineteen(engine: Engine) -> None:
    """V-P6-20 (Brainstorm third): ≥19/20 automatic drafts, every failure with a reason code."""
    ids = [_session(engine, f"rate-{uuid.uuid4().hex[:6]}") for _ in range(20)]
    drafted = [b for b in ids if _document(engine, b) is not None]
    with Session(engine) as s:
        failures = s.execute(
            text(
                "SELECT subject_id, reason_code FROM document_generation_failures "
                "WHERE subject_type = 'brainstorm' AND subject_id = ANY(:ids)"
            ),
            {"ids": ids},
        ).all()
    assert len(drafted) >= 19, (len(drafted), failures)
    assert all(str(r[1]) for r in failures), failures  # every failure carries a stable code
