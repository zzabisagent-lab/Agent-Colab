"""Independent verifier probe for the BLOCKED half of V-P6-24."""

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from server.application.documents import on_implementation_submitted, on_verification_terminal
from server.documents import finalizer
from server.documents.builder import document_id_for_task
from tests.integration.document_seed import DocSeed
from tests.integration.test_document_finalizer_db import engine, seed  # noqa: F401


def test_blocked_attempt_is_preserved_and_not_publishable(engine: Engine, seed: DocSeed) -> None:
    with Session(engine) as session, session.begin():
        task_id = seed.implement(session, "verify24", "Blocked attempt")
        on_implementation_submitted(seed.ctx(session, seed.admin, "verify24-draft"), task_id)
        verification_id = seed.verification(session, task_id, "verify24")
        seed.verdict(session, verification_id, "BLOCKED", "verify24-blocked")
        result = on_verification_terminal(
            seed.ctx(session, seed.admin, "verify24-attempt"), task_id, verification_id
        )
    assert result.data["status"] == "ATTEMPT_FINALIZED"
    document_id = document_id_for_task(task_id)
    with Session(engine) as session:
        assert finalizer.publishable_version(session, document_id) is None
