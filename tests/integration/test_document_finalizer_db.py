"""P6-04 document finalizer.

V-P6-07 automatic draft of a completed Task with 100 % of the mandatory sections;
V-P6-12 the verification section matches the report; V-P6-19 the closure gate;
V-P6-23 the two-stage lifecycle (no verdict in the draft, the draft stays immutable);
V-P6-24 FAILED/BLOCKED keep an ATTEMPT_FINALIZED version and the gates stay closed until PASSED.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from server.application import bus
from server.application.documents import on_implementation_submitted, on_verification_terminal
from server.application.tasks import CompleteTask, load_task
from server.db.engine import make_engine
from server.documents import finalizer
from server.documents.builder import document_id_for_task
from server.documents.lifecycle import (
    DocumentActor,
    expected_document_id,
    finalized_document_check,
    list_versions,
)
from server.documents.templates import SECTION_KEYS, heading_for
from tests.integration.document_seed import DocSeed

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def seed(engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> DocSeed:
    sd = DocSeed("dfin", tmp_path_factory.mktemp("docs"))
    sd.create(engine)
    return sd


def _headings(markdown: str) -> list[str]:
    return [line[3:].strip() for line in markdown.splitlines() if line.startswith("## ")]


def _read(seed: DocSeed, document_id: str, version: int) -> tuple[str, dict[str, Any]]:
    return seed.store.read_version(str(seed.ws), document_id, version)


def test_completed_task_document_has_every_mandatory_section(engine: Engine, seed: DocSeed) -> None:
    """V-P6-07 and V-P6-12: the finalized document carries all sections and the real verdict."""
    with Session(engine) as s, s.begin():
        task_id = seed.implement(s, "fin1", "Finalizer task")
        on_implementation_submitted(seed.ctx(s, seed.admin, "fin1-draft"), task_id)
        vid = seed.verification(s, task_id, "fin1")
        seed.verdict(s, vid, "PASSED", "fin1-verdict", risks=["scope limited to the sample set"])
        result = on_verification_terminal(seed.ctx(s, seed.admin, "fin1-final"), task_id, vid)
    document_id = document_id_for_task(task_id)
    assert result.data["status"] == "FINALIZED"
    markdown, manifest = _read(seed, document_id, int(result.data["version"]))
    assert _headings(markdown) == [heading_for(k, None) for k in SECTION_KEYS]
    # V-P6-12: the verification section reflects the report the verifier submitted
    assert manifest["verification"]["result"] == "PASSED"
    assert f"[[vr:{vid}]]" in markdown and "**PASSED**" in markdown
    assert "V-P6-12: PASS" in markdown
    assert "scope limited to the sample set" in markdown


def test_draft_carries_no_verdict_and_stays_immutable(engine: Engine, seed: DocSeed) -> None:
    """V-P6-23: the pre-verification draft has no verdict; finalizing adds a new version."""
    with Session(engine) as s, s.begin():
        task_id = seed.implement(s, "fin2", "Two-stage task")
        draft = on_implementation_submitted(seed.ctx(s, seed.admin, "fin2-draft"), task_id)
        vid = seed.verification(s, task_id, "fin2")
        seed.verdict(s, vid, "PASSED", "fin2-verdict")
        final = on_verification_terminal(seed.ctx(s, seed.admin, "fin2-final"), task_id, vid)
    document_id = document_id_for_task(task_id)
    draft_md, draft_manifest = _read(seed, document_id, int(draft.data["version"]))
    assert draft_manifest["status"] == "DRAFT_PRE_VERIFICATION"
    assert draft_manifest["verification"] is None
    # no verdict of any kind: the verification section is pending and no result is rendered
    assert "PENDING (pre-verification draft)" in draft_md
    assert "**PASSED**" not in draft_md and "**FAILED**" not in draft_md
    assert "[[vr:" not in draft_md.split("## Verification Method and Results")[1]
    assert int(final.data["version"]) == int(draft.data["version"]) + 1
    # the draft version row is append-only and its stored bytes never change
    with Session(engine) as s, s.begin(), pytest.raises(DBAPIError):
        s.execute(
            text("UPDATE document_versions SET sha256 = :x WHERE document_id = :d AND version = 1"),
            {"x": "0" * 64, "d": document_id},
        )
    assert _read(seed, document_id, int(draft.data["version"]))[0] == draft_md


def test_failed_then_passed_keeps_attempt_and_opens_the_gate(engine: Engine, seed: DocSeed) -> None:
    """V-P6-24 and V-P6-19: FAILED yields ATTEMPT_FINALIZED and a closed gate; PASSED opens it."""
    with Session(engine) as s, s.begin():
        task_id = seed.implement(s, "fin3", "Attempt task")
        on_implementation_submitted(seed.ctx(s, seed.admin, "fin3-draft"), task_id)
        vid = seed.verification(s, task_id, "fin3")
        seed.verdict(s, vid, "FAILED", "fin3-verdict")
        attempt = on_verification_terminal(seed.ctx(s, seed.admin, "fin3-attempt"), task_id, vid)
    assert attempt.data["status"] == "ATTEMPT_FINALIZED"
    document_id = document_id_for_task(task_id)
    attempt_md, attempt_manifest = _read(seed, document_id, int(attempt.data["version"]))
    assert attempt_manifest["verification"]["result"] == "FAILED"
    assert "a new revision is required" in attempt_md
    with Session(engine) as s:
        assert expected_document_id(s, task_id) is None  # the completion gate is closed
        assert finalizer.publishable_version(s, document_id) is None  # publishing is closed too
    with Session(engine) as s, s.begin():
        with pytest.raises(bus.CommandError) as exc:
            bus.execute(
                CompleteTask(task_id, document_id=document_id),
                seed.ctx(s, seed.impl, "fin3-complete-early"),
            )
        # the verification prerequisite fires first while the latest verdict is FAILED
        assert exc.value.code == "VERIFICATION_REQUIRED"
    with Session(engine) as s, s.begin():
        seed.recheck(s, task_id, vid, "fin3-re")
        seed.verdict(s, vid, "PASSED", "fin3-verdict2")
        final = on_verification_terminal(seed.ctx(s, seed.admin, "fin3-final"), task_id, vid)
    assert final.data["status"] == "FINALIZED"
    with Session(engine) as s:
        assert expected_document_id(s, task_id) == document_id
        publishable = finalizer.publishable_version(s, document_id)
        assert publishable is not None and publishable["status"] == "FINALIZED"
        versions = [v["status"] for v in list_versions(s, document_id)]
    # the earlier attempt version is still there, unchanged, next to the finalized one
    assert versions.count("ATTEMPT_FINALIZED") == 1 and versions[-1] == "FINALIZED"
    assert _read(seed, document_id, int(attempt.data["version"]))[0] == attempt_md
    with Session(engine) as s, s.begin():
        bus.execute(
            CompleteTask(task_id, document_id=document_id),
            seed.ctx(s, seed.impl, "fin3-complete"),
        )
    with Session(engine) as s:
        status = s.execute(
            text("SELECT status FROM tasks_projection WHERE task_id = :t"), {"t": task_id}
        ).scalar_one()
    assert status == "COMPLETED"


def test_passed_verification_without_a_finalized_document_closes_the_gate(
    engine: Engine, seed: DocSeed
) -> None:
    """V-P6-19, second half: a PASSED verification alone never completes a Task."""
    import uuid

    with Session(engine) as s, s.begin():
        task_id = seed.implement(s, "fin5", "Gate task")
        # a PASSED run recorded without the documentation pipeline having produced a version
        s.execute(
            text(
                "INSERT INTO verification_runs (id, verification_id, workspace_id, target_type, "
                "target_id, task_id, implementer_account_id, verifier_account_id, "
                "implementer_credential_fingerprint, verifier_credential_fingerprint, "
                "identity_graph_version, effective_policy_hash, criteria_version, target_commit, "
                "status, snapshot_hash, created_by_account_id, current_revision, result) "
                "VALUES (:i, :v, :w, 'task', :t, :t, :im, :ve, 'fp-i', 'fp-v', 'g1', 'p', 'v8.0', "
                "'abc123', 'PASSED', :h, :cb, 1, 'PASSED')"
            ),
            {
                "i": uuid.uuid4(),
                "v": f"vr-gate-{uuid.uuid4().hex[:10]}",
                "w": seed.ws,
                "t": task_id,
                "im": seed.accounts[seed.impl],
                "ve": seed.accounts[seed.ver],
                "h": "1" * 64,
                "cb": seed.accounts[seed.admin],
            },
        )
    with Session(engine) as s:
        assert expected_document_id(s, task_id) is None
        # the registered completion prerequisite refuses while no FINALIZED version exists
        state = load_task(seed.ctx(s, seed.impl, "fin5-state"), task_id)
        assert finalized_document_check(state, s) == "COMPLETION_PREREQUISITE_MISSING"
    with Session(engine) as s, s.begin(), pytest.raises(bus.CommandError) as exc:
        bus.execute(
            CompleteTask(task_id, document_id=document_id_for_task(task_id)),
            seed.ctx(s, seed.impl, "fin5-complete"),
        )
    assert exc.value.code in ("COMPLETION_PREREQUISITE_MISSING", "VERIFICATION_REQUIRED")


def test_source_freeze_is_recorded_and_reproducible(engine: Engine, seed: DocSeed) -> None:
    """The freeze ledger names the exact sources; rebuilding them yields the same manifest hash."""
    with Session(engine) as s, s.begin():
        task_id = seed.implement(s, "fin4", "Freeze task")
        result = finalizer.draft_task(
            s,
            seed.ctx(s, seed.admin, "fin4-draft").store,
            seed.store,
            task_id=task_id,
            actor=_actor(seed, "fin4-draft"),
            now=seed.clock.now(),
        )
    assert result.freeze_id.startswith("frz-")
    with Session(engine) as s:
        row = s.execute(
            text(
                "SELECT subject_type, subject_id, source_manifest::text, manifest_hash "
                "FROM document_freezes WHERE freeze_id = :f"
            ),
            {"f": result.freeze_id},
        ).one()
    assert row[0] == "task" and row[1] == task_id
    manifest = json.loads(str(row[2]))
    assert manifest["subject"] == {"type": "task", "id": task_id}
    assert manifest["event_ids"], "the freeze must name the Events it used"
    from server.documents.provenance import manifest_hash

    assert manifest_hash(manifest) == row[3]


def _actor(seed: DocSeed, key: str) -> DocumentActor:
    return DocumentActor(str(seed.accounts[seed.admin]), f"corr-{seed.tag}", key)
