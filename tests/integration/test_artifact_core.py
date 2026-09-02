"""V-P1-17: Task Artifact metadata/hash/ACL and premature Run link (P1-09)."""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application import artifacts as art
from server.application.bus import CommandContext, CommandError, Principal, execute
from server.artifacts.storage import ArtifactStorage
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore

pytestmark = pytest.mark.db

WS = uuid.uuid4()
WS2 = uuid.uuid4()
CREATOR = uuid.uuid4()
ASSIGNEE = uuid.uuid4()
STRANGER = uuid.uuid4()
OTHER_WS_ACTOR = uuid.uuid4()
CLOCK = FixedClock(dt.datetime(2026, 3, 1, tzinfo=dt.UTC))


class AllowAll:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def require(self, session: Session, principal: str, permission: str, **scope: Any) -> None:
        self.calls.append(permission)


class DenyAll:
    def require(self, session: Session, principal: str, permission: str, **scope: Any) -> None:
        raise CommandError("POLICY_DENIED", permission, 403)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as c:
        for ws, name in ((WS, "ws-art"), (WS2, "ws-art-2")):
            c.execute(
                text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, :w, :w)"),
                {"i": ws, "w": name},
            )
        for acc, ws, name in (
            (CREATOR, WS, "acct-art-creator"),
            (ASSIGNEE, WS, "acct-art-assignee"),
            (STRANGER, WS, "acct-art-stranger"),
            (OTHER_WS_ACTOR, WS2, "acct-art-other"),
        ):
            c.execute(
                text(
                    "INSERT INTO accounts "
                    "(id, account_id, workspace_id, account_type, display_name) "
                    "VALUES (:i, :a, :w, 'agent', :a)"
                ),
                {"i": acc, "a": name, "w": ws},
            )
        for task, ws in (("task-art-1", WS), ("task-art-other", WS2)):
            c.execute(
                text(
                    "INSERT INTO tasks_projection (task_id, workspace_id, root_task_id, title, "
                    "domain, risk, status, assignee_account_id, created_at, updated_at) VALUES "
                    "(:t, :w, :t, 't', 'research', 'LOW', 'RUNNING', :as, now(), now())"
                ),
                {"t": task, "w": ws, "as": ASSIGNEE if ws == WS else OTHER_WS_ACTOR},
            )
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def storage(tmp_path_factory: pytest.TempPathFactory) -> ArtifactStorage:
    return ArtifactStorage(tmp_path_factory.mktemp("artifacts"))


def _ctx(
    session: Session,
    storage: ArtifactStorage,
    actor: uuid.UUID,
    key: str,
    authorizer: Any | None = None,
    ws: uuid.UUID = WS,
) -> CommandContext:
    return CommandContext(
        session=session,
        store=PostgresEventStore(session, clock=CLOCK),
        authorizer=authorizer or AllowAll(),
        clock=CLOCK,
        principal=Principal(f"acct-{actor}", str(actor), "agent", "fp-" + str(actor)[:8]),
        workspace_id=str(ws),
        correlation_id="corr-art",
        idempotency_key=key,
        extras={"artifact_storage": storage},
    )


def test_register_link_read_checksum_and_acl(engine: Engine, storage: ArtifactStorage) -> None:
    content = b"# report\n\nresult: ok\n"
    with Session(engine) as s, s.begin():
        ctx = _ctx(s, storage, CREATOR, "reg-1")
        res = execute(art.RegisterArtifact("report.md", "text/markdown", content=content), ctx)
        assert res.aggregate_type == "artifact" and res.aggregate_seq == 1
        assert res.data["sha256"] == hashlib.sha256(content).hexdigest()
        assert res.data["size"] == len(content)
        artifact_id = res.resource_id
        # idempotent retry replays the same artifact
        again = execute(art.RegisterArtifact("report.md", "text/markdown", content=content), ctx)
        assert again.replayed and again.resource_id == artifact_id
        # link to the existing Task in the same workspace
        link = execute(
            art.LinkArtifact(artifact_id, "task", "task-art-1", "attachment"),
            _ctx(s, storage, CREATOR, "link-1"),
        )
        assert link.data["links"][0]["subject_id"] == "task-art-1"
        # metadata/hash consistent between table, Event, and stored bytes
        row = s.execute(
            text("SELECT sha256, size, mime, status FROM artifacts WHERE artifact_id = :a"),
            {"a": artifact_id},
        ).first()
        assert row is not None and (row[0], row[1], row[2], row[3]) == (
            res.data["sha256"],
            len(content),
            "text/markdown",
            "registered",
        )
        ev = ctx.store.stream(str(WS), "artifact", artifact_id)[0]
        assert ev["type"] == "ARTIFACT_REGISTERED" and ev["payload"]["sha256"] == row[0]
        # reads: creator, linked task assignee -> ok; stranger -> normalized NOT_FOUND
        read = art.read_artifact(_ctx(s, storage, CREATOR, "r1"), artifact_id, with_content=True)
        assert read["content"] == content and read["links"][0]["subject_type"] == "task"
        assert art.read_artifact(_ctx(s, storage, ASSIGNEE, "r2"), artifact_id)["sha256"] == row[0]
        with pytest.raises(CommandError) as exc:
            art.read_artifact(_ctx(s, storage, STRANGER, "r3"), artifact_id)
        assert exc.value.code == "NOT_FOUND" and exc.value.status == 404
        with pytest.raises(CommandError) as exc2:  # policy denial also normalized
            art.read_artifact(_ctx(s, storage, CREATOR, "r4", DenyAll()), artifact_id)
        assert exc2.value.code == "POLICY_DENIED"
        # verify -> ARTIFACT_VERIFIED
        ver = execute(art.VerifyArtifact(artifact_id), _ctx(s, storage, CREATOR, "ver-1"))
        assert ver.data["status"] == "verified" and ver.aggregate_seq == 2
        s.info["artifact_id"] = artifact_id


def test_premature_run_link_and_link_negatives(engine: Engine, storage: ArtifactStorage) -> None:
    with Session(engine) as s, s.begin():
        res = execute(
            art.RegisterArtifact("data.csv", "text/csv", content=b"a,b\n1,2\n"),
            _ctx(s, storage, CREATOR, "reg-2"),
        )
        aid = res.resource_id
        cases = [
            (
                art.LinkArtifact(aid, "schedule_run", "run-0001"),
                "SUBJECT_TYPE_NOT_ACTIVE",
                "Phase 5",
            ),
            (art.LinkArtifact(aid, "brainstorm", "bs-0001"), "SUBJECT_TYPE_NOT_ACTIVE", "Phase 6"),
            (art.LinkArtifact(aid, "decision", "dec-0001"), "SUBJECT_TYPE_NOT_ACTIVE", "Phase 6"),
            (art.LinkArtifact(aid, "workspace", "ws"), "SUBJECT_TYPE_UNKNOWN", ""),
            (art.LinkArtifact(aid, "task", "task-missing"), "SUBJECT_NOT_FOUND", ""),
            (art.LinkArtifact(aid, "task", "task-art-other"), "WORKSPACE_MISMATCH", ""),
            (art.LinkArtifact(aid, "task", "task-art-1", ""), "ARTIFACT_LINK_RELATION_INVALID", ""),
            (art.LinkArtifact("art-" + "0" * 20, "task", "task-art-1"), "NOT_FOUND", ""),
        ]
        for i, (cmd, code, detail) in enumerate(cases):
            with pytest.raises(CommandError) as exc:
                execute(cmd, _ctx(s, storage, CREATOR, f"neg-{i}"))
            assert exc.value.code == code, cmd
            assert detail in exc.value.detail
        assert (
            s.execute(
                text("SELECT count(*) FROM artifact_links WHERE artifact_id = :a"), {"a": aid}
            ).scalar()
            == 0
        )
        execute(art.LinkArtifact(aid, "task", "task-art-1"), _ctx(s, storage, CREATOR, "l-ok"))
        with pytest.raises(CommandError) as dup:
            execute(art.LinkArtifact(aid, "task", "task-art-1"), _ctx(s, storage, CREATOR, "l-dup"))
        assert dup.value.code == "ARTIFACT_LINK_DUPLICATE"
        # another workspace cannot see or link the artifact
        with pytest.raises(CommandError) as foreign:
            execute(
                art.LinkArtifact(aid, "task", "task-art-other"),
                _ctx(s, storage, OTHER_WS_ACTOR, "l-foreign", ws=WS2),
            )
        assert foreign.value.code == "NOT_FOUND"


def test_tampered_bytes_are_detected_and_quarantined(
    engine: Engine, storage: ArtifactStorage
) -> None:
    with Session(engine) as s, s.begin():
        res = execute(
            art.RegisterArtifact("notes.txt", "text/plain", content=b"original"),
            _ctx(s, storage, CREATOR, "reg-3"),
        )
        aid = res.resource_id
        path = storage.path_for(res.data["storage_uri"])
        path.chmod(0o640)
        path.write_bytes(b"tampered")
        with pytest.raises(CommandError) as exc:
            art.read_artifact(_ctx(s, storage, CREATOR, "r-t"), aid, with_content=True)
        assert exc.value.code == "ARTIFACT_CHECKSUM_MISMATCH"
        ver = execute(art.VerifyArtifact(aid), _ctx(s, storage, CREATOR, "ver-t"))
        assert ver.data == {"status": "quarantined", "reason_code": "ARTIFACT_CHECKSUM_MISMATCH"}
        events = [e["type"] for e in PostgresEventStore(s).stream(str(WS), "artifact", aid)]
        assert events == ["ARTIFACT_REGISTERED", "ARTIFACT_QUARANTINED"]
        with pytest.raises(CommandError) as link:
            execute(art.LinkArtifact(aid, "task", "task-art-1"), _ctx(s, storage, CREATOR, "l-q"))
        assert link.value.code == "ARTIFACT_QUARANTINED"


def test_external_registration_validation_and_archive_keeps_acl(
    engine: Engine, storage: ArtifactStorage
) -> None:
    with Session(engine) as s, s.begin():
        with pytest.raises(CommandError) as exc:
            execute(
                art.RegisterArtifact("ext.bin", "application/octet-stream", storage_uri="nas://x"),
                _ctx(s, storage, CREATOR, "ext-bad"),
            )
        assert exc.value.code == "ARTIFACT_METADATA_INVALID"
        with pytest.raises(CommandError) as mime:
            execute(
                art.RegisterArtifact("tool.exe", "application/x-msdownload", content=b"MZ"),
                _ctx(s, storage, CREATOR, "ext-mime"),
            )
        assert mime.value.code == "ARTIFACT_MIME_DENIED"
        ok = execute(
            art.RegisterArtifact(
                "ext.bin",
                "application/octet-stream",
                storage_uri="nas://share/ext.bin",
                sha256="ab" * 32,
                size=10,
                readers=(str(ASSIGNEE),),
            ),
            _ctx(s, storage, CREATOR, "ext-ok"),
        )
        arch = execute(art.ArchiveArtifact(ok.resource_id), _ctx(s, storage, CREATOR, "arch"))
        assert arch.data["status"] == "archived"
        meta = art.read_artifact(_ctx(s, storage, ASSIGNEE, "r-a"), ok.resource_id)
        assert meta["status"] == "archived" and meta["acl"] == {"readers": [str(ASSIGNEE)]}
        with pytest.raises(CommandError) as stranger:
            art.read_artifact(_ctx(s, storage, STRANGER, "r-b"), ok.resource_id)
        assert stranger.value.code == "NOT_FOUND"
        with pytest.raises(CommandError) as ver:
            execute(art.VerifyArtifact(ok.resource_id), _ctx(s, storage, CREATOR, "ver-arch"))
        assert ver.value.code == "ARTIFACT_ARCHIVED"


def test_storage_root_isolated_per_workspace(storage: ArtifactStorage, tmp_path: Path) -> None:
    blob = storage.write_bytes(str(WS), "x.txt", "text/plain", b"x")
    assert str(WS) in str(blob.path) and blob.path.parent.parent.parent == storage.root
