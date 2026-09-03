"""V-P6-06 (DB half): a malicious or tampered Artifact is quarantined with a redacted audit entry,
becomes unreadable through the normal path, and can be released only deliberately (P6-03)."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application import artifacts as art
from server.application.bus import execute
from server.artifacts import quarantine as qtn
from server.artifacts.scan import EICAR, SignatureScanner, report_for
from server.artifacts.storage import ArtifactStorage
from server.artifacts.upload import store_upload
from server.db.engine import make_engine
from tests.integration.phase6_publish_seed import Seed

pytestmark = pytest.mark.db
SEED = Seed("p6qtn")
INFECTED = b"report text " + EICAR + b" trailing text"


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    SEED.install(eng)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def storage(tmp_path_factory: pytest.TempPathFactory) -> ArtifactStorage:
    return ArtifactStorage(
        root=tmp_path_factory.mktemp("p6-qtn-artifacts"), scanner=SignatureScanner()
    )


def _register(engine: Engine, storage: ArtifactStorage, name: str, data: bytes, idem: str) -> str:
    stored = store_upload(
        storage,
        workspace_id=str(SEED.ws),
        filename=name,
        mime="text/plain",
        stream=io.BytesIO(data),
    )
    with Session(engine) as s, s.begin():
        result = execute(
            art.RegisterArtifact(
                filename=stored.filename,
                mime=stored.mime,
                storage_uri=stored.blob.storage_uri,
                sha256=stored.blob.sha256,
                size=stored.blob.size,
            ),
            SEED.context(s, "uploader", idem, storage=storage),
        )
    return result.resource_id


def test_malware_is_quarantined_with_a_redacted_audit(
    engine: Engine, storage: ArtifactStorage
) -> None:
    artifact_id = _register(engine, storage, "infected.txt", INFECTED, "qtn-1")
    stored_uri = None
    with Session(engine) as s, s.begin():
        stored_uri = s.execute(
            text("SELECT storage_uri FROM artifacts WHERE artifact_id = :a"), {"a": artifact_id}
        ).scalar_one()
        report = report_for(storage.scanner, storage.path_for(str(stored_uri)))
        assert report.verdict == "infected"
        qtn.record_scan(s, artifact_id, report, SEED.clock)
        qtn.quarantine(
            s,
            workspace_id=str(SEED.ws),
            artifact_id=artifact_id,
            reason_code=report.reason_code or "ARTIFACT_MALWARE",
            detail=report.detail,
            actor_account_uuid=str(SEED.accounts["uploader"]),
            actor_label=f"acct-{SEED.tag}-uploader",
            correlation_id="corr-qtn",
            clock=SEED.clock,
        )

    with Session(engine) as s:
        status = s.execute(
            text("SELECT status FROM artifacts WHERE artifact_id = :a"), {"a": artifact_id}
        ).scalar_one()
        assert status == "quarantined"
        held = qtn.status_of(s, artifact_id)
        assert held is not None and held.open and held.reason_code == "ARTIFACT_MALWARE"
        scans = qtn.scans_of(s, artifact_id)
        assert scans and scans[0]["verdict"] == "infected"
        row = s.execute(
            text(
                "SELECT action, result, error_code, redacted_metadata FROM audit_events "
                "WHERE workspace_id = :w AND target_id = :a AND action = 'artifact.quarantined'"
            ),
            {"w": SEED.ws, "a": artifact_id},
        ).first()
    assert row is not None
    assert row[0] == "artifact.quarantined" and row[1] == "DENY"
    assert row[2] == "ARTIFACT_MALWARE"
    blob = json.dumps(row[3])
    assert "EICAR-Test-File" in blob  # the signature name is useful provenance
    assert EICAR.decode() not in blob  # the sample itself never reaches the audit trail


def test_quarantined_artifact_cannot_be_linked_and_can_be_released(
    engine: Engine, storage: ArtifactStorage
) -> None:
    artifact_id = _register(engine, storage, "infected2.txt", INFECTED, "qtn-2")
    with Session(engine) as s, s.begin():
        qtn.quarantine(
            s,
            workspace_id=str(SEED.ws),
            artifact_id=artifact_id,
            reason_code="ARTIFACT_MALWARE",
            detail="EICAR-Test-File",
            actor_account_uuid=str(SEED.accounts["uploader"]),
            actor_label=f"acct-{SEED.tag}-uploader",
            correlation_id="corr-qtn-2",
            clock=SEED.clock,
        )
    from server.application.bus import CommandError

    with Session(engine) as s, s.begin(), pytest.raises(CommandError) as exc:
        execute(
            art.LinkArtifact(artifact_id, "task", SEED.task_id, "evidence"),
            SEED.context(s, "uploader", "qtn-link", storage=storage),
        )
    assert exc.value.code == "ARTIFACT_QUARANTINED"

    with Session(engine) as s, s.begin():
        assert qtn.release(
            s,
            workspace_id=str(SEED.ws),
            artifact_id=artifact_id,
            released_by=str(SEED.accounts["publisher"]),
            actor_label=f"acct-{SEED.tag}-publisher",
            reason="reviewed by hand, fixture is a known test string",
            correlation_id="corr-qtn-2",
            clock=SEED.clock,
        )
    with Session(engine) as s, s.begin():
        assert not qtn.release(  # a second release is a no-op, not a second audit entry
            s,
            workspace_id=str(SEED.ws),
            artifact_id=artifact_id,
            released_by=str(SEED.accounts["publisher"]),
            actor_label=f"acct-{SEED.tag}-publisher",
            reason="again",
            correlation_id="corr-qtn-2",
            clock=SEED.clock,
        )
    with Session(engine) as s:
        status = s.execute(
            text("SELECT status FROM artifacts WHERE artifact_id = :a"), {"a": artifact_id}
        ).scalar_one()
        held = qtn.status_of(s, artifact_id)
        released_audits = s.execute(
            text(
                "SELECT count(*) FROM audit_events WHERE workspace_id = :w AND target_id = :a "
                "AND action = 'artifact.quarantine_released'"
            ),
            {"w": SEED.ws, "a": artifact_id},
        ).scalar_one()
    assert status == "registered" and held is not None and not held.open
    assert released_audits == 1
