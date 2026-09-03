"""Independent verifier probe for automatic Brainstorm documents and generation rate."""

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application import brainstorm as bs
from server.domain.clock import FixedClock
from tests.integration.brainstorm_seed import T0
from tests.integration.test_brainstorm_engine_db import SEED, _start, engine  # noqa: F401


def test_twenty_closed_brainstorms_each_create_an_automatic_draft(engine: Engine) -> None:
    clock = FixedClock(T0)
    brainstorm_ids = []
    for number in range(20):
        brainstorm_id = _start(engine, clock, {}, f"verify-rate-start-{number}")
        SEED.run(
            engine,
            bs.CloseBrainstorm(brainstorm_id=brainstorm_id),
            SEED.facilitator,
            f"verify-rate-close-{number}",
            clock,
        )
        brainstorm_ids.append(brainstorm_id)
    with Session(engine) as session:
        drafted = session.execute(
            text(
                "SELECT count(DISTINCT subject_id) FROM document_freezes "
                "WHERE subject_type = 'brainstorm' AND subject_id = ANY(:ids)"
            ),
            {"ids": brainstorm_ids},
        ).scalar_one()
    assert drafted >= 19, f"V-P6-08/V-P6-20 expected >=19 automatic drafts; actual={drafted}"
