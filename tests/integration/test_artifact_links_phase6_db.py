"""V-P6-25 (P6-03): Artifacts link to Task, ScheduleRun, Brainstorm and Decision subjects with the
subject's ACL; a wrong type, an unknown id or another workspace's id gives a stable error and
changes nothing.

The Brainstorm package (P6-02/P6-09) owns ``brainstorms``, ``brainstorm_participants`` and
``brainstorm_decisions`` in migration 0019; this test seeds rows in those real tables.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application import artifacts as art
from server.application.bus import CommandError, execute
from server.artifacts.links import REGISTRY, SUBJECT_TYPES
from server.artifacts.service import can_read, get_artifact, links_of
from server.artifacts.storage import ArtifactStorage
from server.artifacts.upload import store_upload
from server.db.engine import make_engine
from tests.integration.phase6_publish_seed import T0, Seed

pytestmark = pytest.mark.db
SEED = Seed("p6lnk")


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    SEED.install(eng)
    with Session(eng) as s, s.begin():
        s.execute(
            text(
                "INSERT INTO brainstorms (id, brainstorm_id, workspace_id, channel_id, topic, "
                "facilitator_account_id, started_at, created_at, updated_at) VALUES (:i, :b, :w, "
                ":c, 'links', :f, :n, :n, :n)"
            ),
            {
                "i": uuid.uuid4(),
                "b": f"bs-{SEED.tag}",
                "w": SEED.ws,
                "c": SEED.channel,
                "f": SEED.accounts["publisher"],
                "n": T0,
            },
        )
        s.execute(
            text(
                "INSERT INTO brainstorm_participants (brainstorm_id, account_id, role, seat, "
                "joined_at) VALUES (:b, :a, 'human', 1, :n)"
            ),
            {"b": f"bs-{SEED.tag}", "a": SEED.accounts["reviewer"], "n": T0},
        )
        s.execute(
            text(
                "INSERT INTO brainstorm_decisions (decision_id, brainstorm_id, workspace_id, "
                "statement, rationale, decided_by, decided_at, event_id) VALUES (:d, :b, :w, "
                "'decided', 'because', :who, :n, :e)"
            ),
            {
                "d": f"dec-{SEED.tag}",
                "b": f"bs-{SEED.tag}",
                "w": SEED.ws,
                "who": SEED.accounts["publisher"],
                "n": T0,
                "e": SEED.seed_event(s, "BRAINSTORM_OPENED", f"bs-{SEED.tag}"),
            },
        )
        # a Brainstorm belonging to the other workspace, for the mismatch case
        s.execute(
            text(
                "INSERT INTO brainstorms (id, brainstorm_id, workspace_id, channel_id, topic, "
                "facilitator_account_id, started_at, created_at, updated_at) VALUES (:i, :b, :w, "
                ":c, 'other', :f, :n, :n, :n)"
            ),
            {
                "i": uuid.uuid4(),
                "b": f"bs-{SEED.tag}-other",
                "w": SEED.other_ws,
                "c": SEED.channel,
                "f": SEED.accounts["outsider"],
                "n": T0,
            },
        )
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def storage(tmp_path_factory: pytest.TempPathFactory) -> ArtifactStorage:
    return ArtifactStorage(root=tmp_path_factory.mktemp("p6-link-artifacts"))


def _artifact(engine: Engine, storage: ArtifactStorage, idem: str) -> str:
    stored = store_upload(
        storage,
        workspace_id=str(SEED.ws),
        filename=f"{idem}.txt",
        mime="text/plain",
        stream=io.BytesIO(f"body for {idem}".encode()),
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


def test_every_phase_six_subject_type_is_active() -> None:
    status = REGISTRY.status()
    assert set(status) == set(SUBJECT_TYPES)
    assert all(status[t]["active"] for t in SUBJECT_TYPES)
    assert status["brainstorm"]["activating_phase"] == 6
    assert status["decision"]["activating_phase"] == 6


def test_brainstorm_and_decision_links_carry_the_subject_acl(
    engine: Engine, storage: ArtifactStorage
) -> None:
    artifact_id = _artifact(engine, storage, "lnk-bs")
    with Session(engine) as s, s.begin():
        execute(
            art.LinkArtifact(artifact_id, "brainstorm", f"bs-{SEED.tag}", "transcript"),
            SEED.context(s, "uploader", "lnk-bs-1", storage=storage),
        )
    with Session(engine) as s:
        record = get_artifact(s, str(SEED.ws), artifact_id)
        assert record is not None
        # facilitator and participant read through the Brainstorm ACL; a stranger does not
        assert can_read(s, record, str(SEED.accounts["publisher"]))
        assert can_read(s, record, str(SEED.accounts["reviewer"]))
        assert not can_read(s, record, str(SEED.accounts["stranger"]))

    decision_artifact = _artifact(engine, storage, "lnk-dec")
    with Session(engine) as s, s.begin():
        execute(
            art.LinkArtifact(decision_artifact, "decision", f"dec-{SEED.tag}", "rationale"),
            SEED.context(s, "uploader", "lnk-dec-1", storage=storage),
        )
    with Session(engine) as s:
        record = get_artifact(s, str(SEED.ws), decision_artifact)
        assert record is not None
        # a Decision inherits its Brainstorm's readers as well as its recorder
        assert can_read(s, record, str(SEED.accounts["publisher"]))
        assert can_read(s, record, str(SEED.accounts["reviewer"]))
        assert not can_read(s, record, str(SEED.accounts["stranger"]))
        assert [link["subject_type"] for link in links_of(s, decision_artifact)] == ["decision"]


def test_wrong_subject_type_id_or_workspace_changes_nothing(
    engine: Engine, storage: ArtifactStorage
) -> None:
    artifact_id = _artifact(engine, storage, "lnk-neg")
    cases = [
        (art.LinkArtifact(artifact_id, "channel", "chan-x"), "SUBJECT_TYPE_UNKNOWN"),
        (art.LinkArtifact(artifact_id, "brainstorm", "bs-missing"), "SUBJECT_NOT_FOUND"),
        (art.LinkArtifact(artifact_id, "decision", "dec-missing"), "SUBJECT_NOT_FOUND"),
        (
            art.LinkArtifact(artifact_id, "brainstorm", f"bs-{SEED.tag}-other"),
            "WORKSPACE_MISMATCH",
        ),
    ]
    for i, (cmd, code) in enumerate(cases):
        with Session(engine) as s, s.begin(), pytest.raises(CommandError) as exc:
            execute(cmd, SEED.context(s, "uploader", f"lnk-neg-{i}", storage=storage))
        assert exc.value.code == code, cmd
    with Session(engine) as s:
        assert links_of(s, artifact_id) == []  # zero side effects from every rejected attempt


def test_schedule_run_and_task_links_still_work(engine: Engine, storage: ArtifactStorage) -> None:
    """The Phase 1 and Phase 5 subjects keep working beside the new ones (regression)."""
    artifact_id = _artifact(engine, storage, "lnk-task")
    with Session(engine) as s, s.begin():
        execute(
            art.LinkArtifact(artifact_id, "task", SEED.task_id, "evidence"),
            SEED.context(s, "uploader", "lnk-task-1", storage=storage),
        )
    with Session(engine) as s, s.begin(), pytest.raises(CommandError) as exc:
        execute(
            art.LinkArtifact(artifact_id, "schedule_run", "run-missing"),
            SEED.context(s, "uploader", "lnk-run-1", storage=storage),
        )
    assert exc.value.code == "SUBJECT_NOT_FOUND"
    with Session(engine) as s:
        assert [link["subject_type"] for link in links_of(s, artifact_id)] == ["task"]
