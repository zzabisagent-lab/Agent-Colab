"""V-P6-05 (P6-03): upload → readback → hash matches, ACL preserved from the linked subject."""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application import artifacts as art
from server.application.bus import execute
from server.artifacts.service import can_read, get_artifact, links_of
from server.artifacts.storage import ArtifactStorage
from server.artifacts.upload import readback_hash, store_upload
from server.db.engine import make_engine
from tests.integration.phase6_publish_seed import Seed

pytestmark = pytest.mark.db
SEED = Seed("p6up")
BODY = b"artifact,integrity\nhash,preserved\n"


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    SEED.install(eng)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def storage(tmp_path_factory: pytest.TempPathFactory) -> ArtifactStorage:
    return ArtifactStorage(root=tmp_path_factory.mktemp("p6-artifacts"))


def test_upload_readback_hash_and_subject_acl(engine: Engine, storage: ArtifactStorage) -> None:
    expected = hashlib.sha256(BODY).hexdigest()
    stored = store_upload(
        storage,
        workspace_id=str(SEED.ws),
        filename="integrity.csv",
        mime="text/csv",
        stream=io.BytesIO(BODY),
    )
    assert stored.blob.sha256 == expected and stored.sniffed == "text/plain"
    # readback re-reads the stored bytes and recomputes the digest
    assert readback_hash(storage, stored.blob.storage_uri, expected) == expected

    with Session(engine) as s, s.begin():
        result = execute(
            art.RegisterArtifact(
                filename=stored.filename,
                mime=stored.mime,
                storage_uri=stored.blob.storage_uri,
                sha256=stored.blob.sha256,
                size=stored.blob.size,
            ),
            SEED.context(s, "uploader", "up-1", storage=storage),
        )
        artifact_id = result.resource_id
        execute(
            art.LinkArtifact(artifact_id, "task", SEED.task_id, "evidence"),
            SEED.context(s, "uploader", "up-2", storage=storage),
        )

    with Session(engine) as s:
        record = get_artifact(s, str(SEED.ws), artifact_id)
        assert record is not None
        assert record.sha256 == expected and record.size == len(BODY)
        assert storage.read(record.storage_uri, record.sha256) == BODY
        # the ACL comes from the linked subject: the Task assignee reads, a stranger does not
        assert can_read(s, record, str(SEED.accounts["uploader"]))  # creator
        assert can_read(s, record, str(SEED.accounts["reader"]))  # task assignee
        assert not can_read(s, record, str(SEED.accounts["stranger"]))
        assert [link["relation"] for link in links_of(s, artifact_id)] == ["evidence"]


def test_readback_detects_tampering(engine: Engine, storage: ArtifactStorage) -> None:
    """A blob changed underneath keeps its row but fails verification (never silently served)."""
    stored = store_upload(
        storage,
        workspace_id=str(SEED.ws),
        filename="tamper.txt",
        mime="text/plain",
        stream=io.BytesIO(b"original bytes"),
    )
    path: Path = storage.path_for(stored.blob.storage_uri)
    path.chmod(0o640)
    path.write_bytes(b"replaced bytes")
    from server.artifacts.upload import ArtifactUploadError

    with pytest.raises(ArtifactUploadError) as exc:
        readback_hash(storage, stored.blob.storage_uri, stored.blob.sha256)
    assert exc.value.code == "ARTIFACT_CHECKSUM_MISMATCH"


def test_registered_artifact_rows_are_scoped_to_the_workspace(engine: Engine) -> None:
    with Session(engine) as s:
        count = s.execute(
            text("SELECT count(*) FROM artifacts WHERE workspace_id = :w"), {"w": SEED.ws}
        ).scalar_one()
    assert count >= 1
