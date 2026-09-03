"""P1-10: V-P1-18 (pre-verification draft), V-P1-19 (attempt vs. finalized versions and the
completion gate), V-P1-20 (crypto-shredding leaves Event bytes/hash unchanged; document redacts)."""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from server.application import bus
from server.application.authz import AllowAllAuthorizer
from server.application.criteria import current_criteria
from server.application.documents import DraftDocument, FinalizeAttempt
from server.application.tasks import (
    AcceptTask,
    CompleteTask,
    CreateTask,
    DelegateTask,
    StartTask,
    StartVerification,
    SubmitImplementation,
)
from server.application.verification import (
    AssignVerifier,
    CreateVerificationRun,
    RequestRecheck,
    SubmitFix,
    SubmitVerdict,
)
from server.application.verification import StartVerification as StartRun
from server.db.engine import make_engine
from server.documents.builder import document_id_for_task
from server.documents.lifecycle import expected_document_id, list_versions
from server.documents.store import DocumentStore
from server.domain.clock import FixedClock
from server.events.postgres_store import PostgresEventStore
from server.events.store import AppendRequest
from server.secrets.envelope import CryptoError, EnvelopeCrypto, MasterKey, new_master_key

pytestmark = pytest.mark.db

WS = uuid.uuid4()
CHANNEL = uuid.uuid4()
ACCOUNTS: dict[str, uuid.UUID] = {
    n: uuid.uuid4() for n in ("acct-dl-admin", "acct-dl-impl", "acct-dl-ver")
}
CLOCK = FixedClock(dt.datetime(2026, 4, 1, tzinfo=dt.UTC))
CRYPTO = EnvelopeCrypto(MasterKey.from_b64("mk-dl", new_master_key()), CLOCK)
CRITERIA = ({"statement": "report attached", "check_type": "evidence", "required": True},)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-dl', 'dl')"),
            {"i": WS},
        )
        for name, acc in ACCOUNTS.items():
            typ = "service" if name.endswith("admin") else "agent"
            c.execute(
                text(
                    "INSERT INTO accounts "
                    "(id, account_id, workspace_id, account_type, display_name) "
                    "VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc, "a": name, "w": WS, "t": typ},
            )
        c.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name) "
                "VALUES (:i, 'chan-dl', :w, 'work', 'dl')"
            ),
            {"i": CHANNEL, "w": WS},
        )
    yield eng
    eng.dispose()


@pytest.fixture()
def store(tmp_path: Path) -> DocumentStore:
    return DocumentStore(tmp_path / "documents")


def _ctx(session: Session, who: str, key: str, store: DocumentStore) -> bus.CommandContext:
    return bus.CommandContext(
        session=session,
        store=PostgresEventStore(session, crypto=CRYPTO, clock=CLOCK),
        authorizer=AllowAllAuthorizer(),
        clock=CLOCK,
        principal=bus.Principal(who, str(ACCOUNTS[who]), "agent", f"sha256:{who}"),
        workspace_id=str(WS),
        correlation_id="corr-dl",
        idempotency_key=key,
        extras={"document_store": store},
    )


def _report(result: str, risks: list[str] | None = None) -> dict[str, Any]:
    return {
        "result": result,
        "criteria_version": "v8.0",
        "tests": [
            {
                "id": "V-P1-01",
                "result": "PASS" if result == "PASSED" else "FAIL",
                "evidence_ref": "e/1",
            }
        ],
        "findings": []
        if result == "PASSED"
        else [{"id": "F-1", "severity": "High", "summary": "broken"}],
        "residual_risks": risks or [],
    }


def _implement(s: Session, store: DocumentStore, key: str, title: str) -> str:
    """create → delegate → accept → start → submit; returns the task id."""
    created = bus.execute(
        CreateTask(title, str(CHANNEL), "research", criteria=CRITERIA),
        _ctx(s, "acct-dl-admin", f"{key}-create", store),
    )
    task_id: str = created.resource_id
    bus.execute(
        DelegateTask(task_id, "acct-dl-impl"), _ctx(s, "acct-dl-admin", f"{key}-delegate", store)
    )
    bus.execute(AcceptTask(task_id), _ctx(s, "acct-dl-impl", f"{key}-accept", store))
    bus.execute(StartTask(task_id), _ctx(s, "acct-dl-impl", f"{key}-start", store))
    _submit(s, store, task_id, f"{key}-submit")
    return task_id


def _submit(s: Session, store: DocumentStore, task_id: str, key: str) -> None:
    current = current_criteria(s, task_id)
    refs = tuple(f"{c.criteria_id}:evidence/{key}" for c in current.criteria)
    bus.execute(
        SubmitImplementation(task_id, refs, criteria_revision=current.revision),
        _ctx(s, "acct-dl-impl", key, store),
    )


def _verification(s: Session, store: DocumentStore, task_id: str, key: str) -> str:
    run = bus.execute(
        CreateVerificationRun(
            target_type="task",
            target_id=task_id,
            implementer_account_id="acct-dl-impl",
            verifier_account_id="acct-dl-ver",
            implementer_credential_fingerprint="sha256:acct-dl-impl",
            verifier_credential_fingerprint="sha256:acct-dl-ver",
            target_commit="abc",
            effective_policy_hash="p",
            task_id=task_id,
        ),
        _ctx(s, "acct-dl-admin", f"{key}-vr", store),
    )
    vid: str = run.resource_id
    bus.execute(
        StartVerification(task_id, vid), _ctx(s, "acct-dl-admin", f"{key}-verifying", store)
    )
    bus.execute(AssignVerifier(vid), _ctx(s, "acct-dl-admin", f"{key}-assign", store))
    bus.execute(StartRun(vid), _ctx(s, "acct-dl-ver", f"{key}-run", store))
    return vid


def _verdict(
    s: Session,
    store: DocumentStore,
    vid: str,
    result: str,
    key: str,
    risks: list[str] | None = None,
) -> None:
    bus.execute(
        SubmitVerdict(vid, result, _report(result, risks)), _ctx(s, "acct-dl-ver", key, store)
    )


def _recheck(s: Session, store: DocumentStore, task_id: str, vid: str, key: str) -> None:
    """After FAILED (RUNNING) or BLOCKED (WAITING): resubmit, re-enter VERIFYING, recheck."""
    status = s.execute(
        text("SELECT status FROM tasks_projection WHERE task_id = :t"), {"t": task_id}
    ).scalar_one()
    if status == "WAITING":
        bus.execute(StartTask(task_id), _ctx(s, "acct-dl-impl", f"{key}-resume", store))
    _submit(s, store, task_id, f"{key}-resubmit")
    bus.execute(
        StartVerification(task_id, vid), _ctx(s, "acct-dl-admin", f"{key}-verifying", store)
    )
    bus.execute(SubmitFix(vid, "def456"), _ctx(s, "acct-dl-impl", f"{key}-fix", store))
    bus.execute(RequestRecheck(vid), _ctx(s, "acct-dl-admin", f"{key}-recheck", store))
    bus.execute(StartRun(vid), _ctx(s, "acct-dl-ver", f"{key}-run", store))


def _read(store: DocumentStore, doc_id: str, version: int) -> tuple[str, dict[str, Any]]:
    return store.read_version(str(WS), doc_id, version)


def test_two_stage_lifecycle_and_completion_gate(engine: Engine, store: DocumentStore) -> None:
    with Session(engine) as s, s.begin():
        task_id = _implement(s, store, "dl1", "Lifecycle task")
        doc_id = document_id_for_task(task_id)

        # ---- V-P1-18: draft after implementation submit
        draft = bus.execute(DraftDocument(task_id), _ctx(s, "acct-dl-admin", "dl1-draft", store))
        assert draft.resource_id == doc_id and draft.data["status"] == "DRAFT_PRE_VERIFICATION"
        md1, mf1 = _read(store, doc_id, 1)
        assert "Result: PENDING (pre-verification draft)" in md1
        assert "**PASSED**" not in md1 and "**FAILED**" not in md1
        assert mf1["provenance"]["task_id"] == task_id and mf1["provenance"]["event_ids"]
        assert mf1["verification"] is None
        ev = s.execute(
            text("SELECT type, payload->>'sha256' FROM events WHERE aggregate_id = :d"),
            {"d": doc_id},
        ).all()
        assert [tuple(r) for r in ev] == [("DOCUMENT_DRAFTED", mf1["sha256"])]
        replay = bus.execute(DraftDocument(task_id), _ctx(s, "acct-dl-admin", "dl1-draft", store))
        assert replay.replayed and replay.data["version"] == 1

        # ---- V-P1-19: FAILED -> ATTEMPT_FINALIZED, not completed
        vid = _verification(s, store, task_id, "dl1")
        with pytest.raises(bus.CommandError) as early:
            bus.execute(FinalizeAttempt(task_id, vid), _ctx(s, "acct-dl-admin", "dl1-early", store))
        assert early.value.code == "VERIFICATION_NOT_TERMINAL"
        _verdict(s, store, vid, "FAILED", "dl1-fail", ["flaky test"])
        att = bus.execute(
            FinalizeAttempt(task_id, vid), _ctx(s, "acct-dl-admin", "dl1-att1", store)
        )
        assert att.data["status"] == "ATTEMPT_FINALIZED" and att.data["version"] == 2
        md2, mf2 = _read(store, doc_id, 2)
        assert "**FAILED**" in md2 and "Residual risk: flaky test" in md2 and "Finding" in md2
        assert mf2["verification"]["result"] == "FAILED"
        with pytest.raises(bus.CommandError) as exc:
            bus.execute(
                CompleteTask(task_id, doc_id), _ctx(s, "acct-dl-admin", "dl1-complete-1", store)
            )
        assert exc.value.code == "COMPLETION_PREREQUISITE_MISSING"
        assert expected_document_id(s, task_id) is None
        again = bus.execute(
            FinalizeAttempt(task_id, vid), _ctx(s, "acct-dl-admin", "dl1-att1-again", store)
        )
        assert again.replayed and again.data["version"] == 2  # one version per terminal revision

        # ---- BLOCKED -> ATTEMPT_FINALIZED v4 (v3 = automatic draft of the re-submission)
        _recheck(s, store, task_id, vid, "dl1-r1")
        _verdict(s, store, vid, "BLOCKED", "dl1-block")
        att2 = bus.execute(
            FinalizeAttempt(task_id, vid), _ctx(s, "acct-dl-admin", "dl1-att2", store)
        )
        assert att2.data["status"] == "ATTEMPT_FINALIZED" and att2.data["version"] == 4
        assert att2.replayed  # the terminal verdict already produced it automatically
        md3, _ = _read(store, doc_id, 4)
        assert "**BLOCKED**" in md3 and "Earlier attempts: revision 1 FAILED" in md3
        with pytest.raises(bus.CommandError) as exc2:
            bus.execute(
                CompleteTask(task_id, doc_id), _ctx(s, "acct-dl-admin", "dl1-complete-2", store)
            )
        assert exc2.value.code == "COMPLETION_PREREQUISITE_MISSING"

        # ---- PASSED -> FINALIZED v6 (v5 = automatic draft) -> completion allowed
        _recheck(s, store, task_id, vid, "dl1-r2")
        _verdict(s, store, vid, "PASSED", "dl1-pass", ["monitor in production"])
        fin = bus.execute(FinalizeAttempt(task_id, vid), _ctx(s, "acct-dl-admin", "dl1-fin", store))
        assert fin.data["status"] == "FINALIZED" and fin.data["version"] == 6 and fin.replayed
        md4, mf4 = _read(store, doc_id, 6)
        assert "**PASSED**" in md4 and mf4["status"] == "FINALIZED"
        assert "Residual risk: monitor in production" in md4
        assert expected_document_id(s, task_id) == doc_id
        done = bus.execute(
            CompleteTask(task_id, doc_id), _ctx(s, "acct-dl-admin", "dl1-complete-4", store)
        )
        assert done.data["status"] == "COMPLETED"

        versions = list_versions(s, doc_id)
        assert [v["status"] for v in versions] == [
            "DRAFT_PRE_VERIFICATION",
            "ATTEMPT_FINALIZED",
            "DRAFT_PRE_VERIFICATION",
            "ATTEMPT_FINALIZED",
            "DRAFT_PRE_VERIFICATION",
            "FINALIZED",
        ]
        assert [v["verification_result"] for v in versions] == [
            None,
            "FAILED",
            None,
            "BLOCKED",
            None,
            "PASSED",
        ]
        types = (
            s.execute(
                text("SELECT type FROM events WHERE aggregate_id = :d ORDER BY aggregate_seq"),
                {"d": doc_id},
            )
            .scalars()
            .all()
        )
        assert types == [
            "DOCUMENT_DRAFTED",
            "DOCUMENT_ATTEMPT_FINALIZED",
            "DOCUMENT_DRAFTED",
            "DOCUMENT_ATTEMPT_FINALIZED",
            "DOCUMENT_DRAFTED",
            "DOCUMENT_FINALIZED",
        ]
        # earlier versions byte-identical (store) and DB rows unchanged
        assert _read(store, doc_id, 1) == (md1, mf1) and _read(store, doc_id, 2) == (md2, mf2)
        for v in versions:
            _body, manifest = _read(store, doc_id, int(v["version"]))
            assert manifest["sha256"] == v["sha256"]
    with pytest.raises(DBAPIError, match="IMMUTABLE_ROW"), engine.begin() as c:
        c.execute(
            text(
                "UPDATE document_versions SET status = 'FINALIZED' "
                "WHERE document_id = :d AND version = 1"
            ),
            {"d": doc_id},
        )


def test_crypto_shredding_keeps_event_bytes_and_document_redacts(
    engine: Engine, store: DocumentStore
) -> None:
    canary = "CANARY-NOT-A-SECRET-0077"
    with Session(engine) as s, s.begin():
        created = bus.execute(
            CreateTask("Sensitive task", str(CHANNEL), "research", criteria=CRITERIA),
            _ctx(s, "acct-dl-admin", "dl2-create", store),
        )
        task_id = created.resource_id
        bus.execute(
            DelegateTask(task_id, "acct-dl-impl"), _ctx(s, "acct-dl-admin", "dl2-delegate", store)
        )
        bus.execute(AcceptTask(task_id), _ctx(s, "acct-dl-impl", "dl2-accept", store))
        bus.execute(StartTask(task_id), _ctx(s, "acct-dl-impl", "dl2-start", store))
        st = PostgresEventStore(s, crypto=CRYPTO, clock=CLOCK)
        stream = st.stream(str(WS), "task", task_id)
        res = st.append(
            AppendRequest(
                workspace_id=str(WS),
                aggregate_type="task",
                aggregate_id=task_id,
                type="TASK_PROGRESS_REPORTED",
                actor_account_id=str(ACCOUNTS["acct-dl-impl"]),
                correlation_id="corr-dl",
                idempotency_scope="task:progress",
                idempotency_key="dl2-sensitive",
                payload={"task_id": task_id, "summary": "credentials obtained"},
                task_id=task_id,
                channel_id=str(CHANNEL),
                sensitive={"credential_hint": canary},
                expected_seq=len(stream) + 1,
            )
        )
        ev = st.get(res.event_id)
        assert ev is not None and ev["sensitive_payload_key_ref"]
        key_ref = ev["sensitive_payload_key_ref"]
        _submit(s, store, task_id, "dl2-submit")
        doc_id = document_id_for_task(task_id)
        bus.execute(DraftDocument(task_id), _ctx(s, "acct-dl-admin", "dl2-draft", store))
        md1, mf1 = _read(store, doc_id, 1)
        assert canary not in md1 and canary not in str(mf1)
        assert "[sensitive content: encrypted, not rendered]" in md1
        assert mf1["provenance"]["sensitive_event_ids"] == [res.event_id]
        before = s.execute(
            text(
                "SELECT content_hash, sensitive_payload_ciphertext, payload, previous_hash "
                "FROM events WHERE event_id = :e"
            ),
            {"e": res.event_id},
        ).first()
        CRYPTO.destroy(s, key_ref, str(ACCOUNTS["acct-dl-admin"]), "hard delete test")
        after = s.execute(
            text(
                "SELECT content_hash, sensitive_payload_ciphertext, payload, previous_hash "
                "FROM events WHERE event_id = :e"
            ),
            {"e": res.event_id},
        ).first()
        assert before is not None and after is not None and tuple(before) == tuple(after)
        with pytest.raises(CryptoError) as exc:
            CRYPTO.decrypt(s, key_ref, base64.b64decode(ev["sensitive_payload_ciphertext"]))
        assert exc.value.code == "KEY_DESTROYED"
        second = bus.execute(DraftDocument(task_id), _ctx(s, "acct-dl-admin", "dl2-draft-2", store))
        assert second.data["version"] == 2
        md2, _ = _read(store, doc_id, 2)
        assert "[sensitive content: redacted by crypto-shredding]" in md2 and canary not in md2
        assert _read(store, doc_id, 1) == (md1, mf1)
