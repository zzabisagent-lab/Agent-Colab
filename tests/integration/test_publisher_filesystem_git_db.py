"""V-P6-15/16/17/18 (P6-06/P6-07): the Git and filesystem publishers keep version and checksum
consistent, a destination outage preserves the canonical document and recovers exactly once, a
manual correction becomes a new version that keeps the original and its reason, and an
unauthorized Agent cannot publish."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application import publishing as pub
from server.application.bus import CommandError, execute
from server.db.engine import make_engine
from server.documents.store import DocumentStore
from tests.integration.phase6_publish_seed import DenyPermissions, Seed, bare_git_remote

pytestmark = pytest.mark.db
SEED = Seed("p6pub")
GIT_DEST = "dest-git"
FS_DEST = "dest-fs"


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    SEED.install(eng)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def roots(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    base = tmp_path_factory.mktemp("p6-publish")
    return {
        "documents": base / "documents",
        "git": base / "git",
        "fs": base / "nas",
        "workdir": base / "clone",
    }


@pytest.fixture(scope="module")
def store(roots: dict[str, Path]) -> DocumentStore:
    return DocumentStore(root=roots["documents"])


@pytest.fixture(scope="module")
def remote(roots: dict[str, Path]) -> str:
    roots["git"].mkdir(parents=True, exist_ok=True)
    return bare_git_remote(roots["git"])


def _extras(roots: dict[str, Path], store: DocumentStore, remote: str, **fail: bool) -> dict:
    """Publisher configuration for this test run, including the outage seam."""
    return {
        "document_store": store,
        "publisher_config_overrides": {
            GIT_DEST: {
                "remote": remote,
                "workdir": str(roots["workdir"]),
                "branch": "main",
                "_fail": fail.get("git", False),
            },
            FS_DEST: {"root": str(roots["fs"]), "_fail": fail.get("fs", False)},
        },
    }


@pytest.fixture(scope="module")
def destinations(engine: Engine, roots: dict[str, Path], remote: str) -> None:
    with Session(engine) as s, s.begin():
        execute(
            pub.RegisterPublishDestination(
                destination_id=GIT_DEST,
                kind="git",
                display_name="Git remote",
                config={"remote": remote, "workdir": str(roots["workdir"]), "branch": "main"},
            ),
            SEED.context(s, "publisher", "dest-1"),
        )
        execute(
            pub.RegisterPublishDestination(
                destination_id=FS_DEST,
                kind="filesystem",
                display_name="NAS",
                config={"root": str(roots["fs"])},
            ),
            SEED.context(s, "publisher", "dest-2"),
        )


def _review(engine: Engine, version: int, idem: str, decision: str = "APPROVED") -> None:
    with Session(engine) as s, s.begin():
        execute(
            pub.ReviewDocumentPublish(
                document_id=SEED.document_id,
                version=version,
                decision=decision,
                reason="checked against the sources",
            ),
            SEED.context(s, "reviewer", idem),
        )


def test_git_publish_update_verify_archive(
    engine: Engine, store: DocumentStore, roots: dict[str, Path], remote: str, destinations: None
) -> None:
    """V-P6-15: publish → verify → archive keep version and checksum consistent."""
    version, sha = SEED.finalized_document(engine, store, version=1)
    _review(engine, version, "rev-1")
    extras = _extras(roots, store, remote)
    with Session(engine) as s, s.begin():
        published = execute(
            pub.PublishDocument(SEED.document_id, version, GIT_DEST),
            SEED.context(s, "publisher", "pub-1", extras=extras),
        )
    assert published.data["checksum"] == sha
    external_ref = published.data["external_ref"]
    assert external_ref.startswith("git://")

    with Session(engine) as s, s.begin():
        verified = execute(
            pub.VerifyPublishedDocument(SEED.document_id, version, GIT_DEST),
            SEED.context(s, "publisher", "ver-1", extras=extras),
        )
    assert verified.data["ok"] and verified.data["checksum"] == sha

    with Session(engine) as s, s.begin():
        archived = execute(
            pub.ArchivePublishedDocument(SEED.document_id, version, GIT_DEST),
            SEED.context(s, "publisher", "arc-1", extras=extras),
        )
    assert archived.data["state"] == "archived"
    with Session(engine) as s:
        row = s.execute(
            text(
                "SELECT state, external_ref, checksum FROM published_documents "
                "WHERE document_id = :d AND version = :v AND destination_id = :dest"
            ),
            {"d": SEED.document_id, "v": version, "dest": GIT_DEST},
        ).first()
    assert row is not None and row[0] == "archived" and row[1] == external_ref and row[2] == sha


def test_outage_preserves_canonical_and_publishes_exactly_once(
    engine: Engine, store: DocumentStore, roots: dict[str, Path], remote: str, destinations: None
) -> None:
    """V-P6-16: while the destination is down nothing is published; recovery publishes once."""
    version, sha = SEED.finalized_document(engine, store, version=2)
    _review(engine, version, "rev-2")
    down = _extras(roots, store, remote, fs=True)
    with Session(engine) as s, s.begin(), pytest.raises(CommandError) as exc:
        execute(
            pub.PublishDocument(SEED.document_id, version, FS_DEST),
            SEED.context(s, "publisher", "pub-down-1", extras=down),
        )
    assert exc.value.code == "PUBLISH_DESTINATION_UNAVAILABLE"

    with Session(engine) as s:
        # the canonical version is untouched and nothing is recorded as published
        markdown, _manifest = store.read_version(str(SEED.ws), SEED.document_id, version)
        assert markdown.startswith(f"# {SEED.document_id} v{version}")
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM published_documents WHERE document_id = :d "
                    "AND version = :v"
                ),
                {"d": SEED.document_id, "v": version},
            ).scalar_one()
            == 0
        )

    up = _extras(roots, store, remote)
    with Session(engine) as s, s.begin():
        first = execute(
            pub.PublishDocument(SEED.document_id, version, FS_DEST),
            SEED.context(s, "publisher", "pub-up-1", extras=up),
        )
    with Session(engine) as s, s.begin():
        again = execute(  # a repeated call after recovery must not publish a second time
            pub.PublishDocument(SEED.document_id, version, FS_DEST),
            SEED.context(s, "publisher", "pub-up-2", extras=up),
        )
    assert again.replayed and again.data["already_published"]
    assert again.data["external_ref"] == first.data["external_ref"]

    with Session(engine) as s:
        rows = s.execute(
            text(
                "SELECT count(*) FROM published_documents WHERE document_id = :d AND version = :v "
                "AND destination_id = :dest"
            ),
            {"d": SEED.document_id, "v": version, "dest": FS_DEST},
        ).scalar_one()
        attempts = s.execute(
            text(
                "SELECT attempt_no, ok, error_code FROM publish_attempts WHERE document_id = :d "
                "AND version = :v AND destination_id = :dest ORDER BY attempt_no"
            ),
            {"d": SEED.document_id, "v": version, "dest": FS_DEST},
        ).all()
    assert rows == 1  # exactly once
    assert [(a[0], a[1]) for a in attempts] == [(1, False), (2, True)]
    assert attempts[0][2] == "PUBLISH_DESTINATION_UNAVAILABLE"
    # the file on the destination matches the canonical checksum
    published_path = roots["fs"] / str(SEED.ws) / SEED.document_id / f"v{version}.md"
    import hashlib

    assert hashlib.sha256(published_path.read_bytes()).hexdigest() == sha


def test_manual_correction_is_a_new_version_keeping_the_original(
    engine: Engine, store: DocumentStore, roots: dict[str, Path], remote: str, destinations: None
) -> None:
    """V-P6-17: a factual correction publishes v4 recording what it corrects and why; v3 stays."""
    original, original_sha = SEED.finalized_document(engine, store, version=3)
    _review(engine, original, "rev-3")
    extras = _extras(roots, store, remote)
    with Session(engine) as s, s.begin():
        execute(
            pub.PublishDocument(SEED.document_id, original, FS_DEST),
            SEED.context(s, "publisher", "pub-3", extras=extras),
        )
    corrected, corrected_sha = SEED.finalized_document(
        engine, store, version=4, body="# corrected\n\nThe figure was 42, not 24.\n"
    )
    _review(engine, corrected, "rev-4")
    with Session(engine) as s, s.begin():
        result = execute(
            pub.PublishDocument(
                SEED.document_id,
                corrected,
                FS_DEST,
                correction_of_version=original,
                correction_reason="figure corrected after review",
            ),
            SEED.context(s, "publisher", "pub-4", extras=extras),
        )
    assert result.data["correction_of_version"] == original
    with Session(engine) as s:
        rows = s.execute(
            text(
                "SELECT version, checksum, correction_of_version, correction_reason, state "
                "FROM published_documents WHERE document_id = :d AND destination_id = :dest "
                "ORDER BY version"
            ),
            {"d": SEED.document_id, "dest": FS_DEST},
        ).all()
    by_version = {r[0]: r for r in rows}
    assert by_version[original][1] == original_sha and by_version[original][4] == "published"
    assert by_version[corrected][1] == corrected_sha
    assert by_version[corrected][2] == original
    assert by_version[corrected][3] == "figure corrected after review"
    # both canonical versions remain readable and distinct
    assert (
        store.read_version(str(SEED.ws), SEED.document_id, original)[0]
        != store.read_version(str(SEED.ws), SEED.document_id, corrected)[0]
    )


def test_publish_requires_authority_and_an_approved_review(
    engine: Engine, store: DocumentStore, roots: dict[str, Path], remote: str, destinations: None
) -> None:
    """V-P6-18: an Agent without document.publish is rejected; so is an unreviewed version."""
    version, _sha = SEED.finalized_document(engine, store, version=5)
    extras = _extras(roots, store, remote)
    with Session(engine) as s, s.begin(), pytest.raises(CommandError) as unreviewed:
        execute(
            pub.PublishDocument(SEED.document_id, version, FS_DEST),
            SEED.context(s, "publisher", "pub-5a", extras=extras),
        )
    assert unreviewed.value.code == "PUBLISH_REVIEW_REQUIRED"

    _review(engine, version, "rev-5")
    with Session(engine) as s, s.begin(), pytest.raises(CommandError) as denied:
        execute(
            pub.PublishDocument(SEED.document_id, version, FS_DEST),
            SEED.context(
                s,
                "agent",
                "pub-5b",
                authorizer=DenyPermissions("document.publish"),
                extras=extras,
            ),
        )
    assert denied.value.code == "POLICY_DENIED"

    with Session(engine) as s:
        assert (
            s.execute(
                text(
                    "SELECT count(*) FROM published_documents WHERE document_id = :d "
                    "AND version = :v"
                ),
                {"d": SEED.document_id, "v": version},
            ).scalar_one()
            == 0
        )

    # a rejected review does not unlock publishing either
    version6, _ = SEED.finalized_document(engine, store, version=6)
    _review(engine, version6, "rev-6", decision="REJECTED")
    with Session(engine) as s, s.begin(), pytest.raises(CommandError) as rejected:
        execute(
            pub.PublishDocument(SEED.document_id, version6, FS_DEST),
            SEED.context(s, "publisher", "pub-6", extras=extras),
        )
    assert rejected.value.code == "PUBLISH_REVIEW_REQUIRED"
