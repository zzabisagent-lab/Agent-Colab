"""Independent verifier probe for the exact V-P6-19 error-code criterion."""

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from server.application import bus
from server.application.documents import on_implementation_submitted, on_verification_terminal
from server.application.tasks import CompleteTask
from server.documents.builder import document_id_for_task
from tests.integration.document_seed import DocSeed
from tests.integration.test_document_finalizer_db import engine, seed  # noqa: F401


def test_missing_latest_passed_verification_uses_required_code(
    engine: Engine, seed: DocSeed
) -> None:
    with Session(engine) as session, session.begin():
        task_id = seed.implement(session, "verify19", "Exact gate code")
        on_implementation_submitted(seed.ctx(session, seed.admin, "verify19-draft"), task_id)
        verification_id = seed.verification(session, task_id, "verify19")
        seed.verdict(session, verification_id, "FAILED", "verify19-failed")
        on_verification_terminal(
            seed.ctx(session, seed.admin, "verify19-attempt"), task_id, verification_id
        )
    with Session(engine) as session, session.begin():
        try:
            bus.execute(
                CompleteTask(task_id, document_id=document_id_for_task(task_id)),
                seed.ctx(session, seed.impl, "verify19-complete"),
            )
        except bus.CommandError as exc:
            assert exc.code == "COMPLETION_PREREQUISITE_MISSING", (
                f"V-P6-19 requires COMPLETION_PREREQUISITE_MISSING; actual={exc.code}"
            )
        else:
            raise AssertionError("completion unexpectedly succeeded")
