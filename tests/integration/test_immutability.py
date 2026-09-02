"""V-P1-05 (Event immutability) and V-P1-25 (audit/verification immutability + chain tamper)."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from server.db.engine import ADMIN_ROLE, RUNTIME_ROLE, make_engine, make_engine_for_role
from server.events.chain import (
    AUDIT_CHAIN,
    VERIFICATION_CHAIN,
    chain_hash,
    hashed_row_fields,
    record_anchor,
    verify_anchors,
    verify_chain,
)
from server.events.hashing import compute_content_hash
from server.observability.audit import append_audit

pytestmark = pytest.mark.db

WS = uuid.uuid4()
ACTOR = uuid.uuid4()
VERIFIER = uuid.uuid4()


@pytest.fixture(scope="module")
def owner(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-imm', 'imm')"),
            {"i": WS},
        )
        for acc, name in ((ACTOR, "acct-imm-actor"), (VERIFIER, "acct-imm-verifier")):
            c.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, "
                    "display_name) VALUES (:i, :a, :w, 'service', :a)"
                ),
                {"i": acc, "a": name, "w": WS},
            )
        ev = {
            "event_id": "evt-" + "a" * 32,
            "schema_version": 1,
            "workspace_id": str(WS),
            "aggregate_type": "task",
            "aggregate_id": "task-imm-1",
            "aggregate_seq": 1,
            "channel_id": None,
            "task_id": "task-imm-1",
            "type": "TASK_CREATED",
            "actor_account_id": str(ACTOR),
            "caused_by": None,
            "correlation_id": "corr-imm",
            "idempotency_scope": "task:create",
            "idempotency_key": "imm-1",
            "policy_version": "policy-v1",
            "payload": {
                "task_id": "task-imm-1",
                "root_task_id": "task-imm-1",
                "channel_id": "c",
                "title": "t",
                "domain": "d",
                "risk": "LOW",
            },
            "sensitive_payload_ciphertext": None,
            "sensitive_payload_key_ref": None,
            "previous_hash": None,
            "occurred_at": "2026-01-01T00:00:00.000Z",
        }
        ev["content_hash"] = compute_content_hash(ev)
        c.execute(
            text(
                "INSERT INTO events (id, event_id, schema_version, workspace_id, aggregate_type, "
                "aggregate_id, aggregate_seq, "
                "task_id, type, actor_account_id, correlation_id, idempotency_scope, "
                "idempotency_key, request_body_hash, "
                "policy_version, payload, content_hash, occurred_at) VALUES (:id, :event_id, 1, "
                ":ws, 'task', :agg, 1, :task, "
                ":type, :actor, :corr, :scope, :key, 'rbh', 'policy-v1', CAST(:payload AS jsonb), "
                ":hash, :occ)"
            ),
            {
                "id": uuid.uuid4(),
                "event_id": ev["event_id"],
                "ws": WS,
                "agg": ev["aggregate_id"],
                "task": ev["task_id"],
                "type": ev["type"],
                "actor": ACTOR,
                "corr": ev["correlation_id"],
                "scope": ev["idempotency_scope"],
                "key": ev["idempotency_key"],
                "payload": json.dumps(ev["payload"]),
                "hash": ev["content_hash"],
                "occ": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            },
        )
        c.execute(
            text(
                "INSERT INTO verification_runs (id, verification_id, workspace_id, target_type, "
                "target_id, implementer_account_id, "
                "verifier_account_id, implementer_credential_fingerprint, "
                "verifier_credential_fingerprint, identity_graph_version, "
                "effective_policy_hash, criteria_version, target_commit, snapshot_hash, "
                "created_by_account_id) VALUES (:id, 'vr-imm-1', :ws, "
                "'task', 'task-imm-1', :impl, :ver, 'fp-a', 'fp-b', 'identity-v8-001', 'sha256:p', "
                "'v8.0', 'abc', 'snap', :impl)"
            ),
            {"id": uuid.uuid4(), "ws": WS, "impl": ACTOR, "ver": VERIFIER},
        )
    yield eng
    eng.dispose()


def _add_revision(engine: Engine, n: int) -> None:
    with engine.begin() as c:
        previous = c.execute(
            text("SELECT content_hash FROM verification_revisions ORDER BY id DESC LIMIT 1")
        ).scalar()
        fields = {
            "revision_id": f"vrr-imm-{n}",
            "verification_id": "vr-imm-1",
            "revision": n,
            "result": "FAILED" if n == 1 else "PASSED",
            "submitted_by_account_id": VERIFIER,
            "submitter_credential_fingerprint": "fp-b",
            "report_sha256": "0" * 64,
            "event_id": "evt-" + "a" * 32,
            "created_at": dt.datetime(2026, 1, 1, 0, 0, n, tzinfo=dt.UTC),
        }
        content_hash = chain_hash(hashed_row_fields(VERIFICATION_CHAIN, fields), previous)
        c.execute(
            text(
                "INSERT INTO verification_revisions (revision_id, verification_id, revision, "
                "result, submitted_by_account_id, "
                "submitter_credential_fingerprint, report, report_sha256, event_id, previous_hash, "
                "content_hash, created_at) VALUES "
                "(:revision_id, :verification_id, :revision, :result, :submitted_by_account_id, "
                ":submitter_credential_fingerprint, "
                "'{}'::jsonb, :report_sha256, :event_id, :prev, :hash, :created_at)"
            ),
            {**fields, "prev": previous, "hash": content_hash},
        )


@pytest.mark.parametrize("role", [RUNTIME_ROLE, ADMIN_ROLE])
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE events SET type = 'TASK_CANCELLED' WHERE event_id = :e",
        "DELETE FROM events WHERE event_id = :e",
        "UPDATE audit_events SET result = 'forged'",
        "DELETE FROM audit_events",
        "UPDATE verification_revisions SET result = 'PASSED'",
        "DELETE FROM verification_revisions",
        "UPDATE audit_hash_anchors SET anchor_hash = 'x'",
        "DELETE FROM key_tombstones",
    ],
)
def test_app_roles_cannot_modify_authority_tables(
    owner: Engine, database_url: str, role: str, statement: str
) -> None:
    eng = make_engine_for_role(database_url, role)
    try:
        with pytest.raises(ProgrammingError, match="permission denied"), eng.begin() as c:
            c.execute(text(statement), {"e": "evt-" + "a" * 32})
    finally:
        eng.dispose()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE events SET type = 'TASK_CANCELLED' WHERE event_id = :e",
        "DELETE FROM events WHERE event_id = :e",
    ],
)
def test_even_the_owner_is_blocked_by_triggers(owner: Engine, statement: str) -> None:
    with pytest.raises(DBAPIError, match="IMMUTABLE_ROW"), owner.begin() as c:
        c.execute(text(statement), {"e": "evt-" + "a" * 32})
    with owner.connect() as c:
        assert (
            c.execute(
                text("SELECT type FROM events WHERE event_id = :e"), {"e": "evt-" + "a" * 32}
            ).scalar()
            == "TASK_CREATED"
        )


def test_audit_chain_appends_and_anchors_verify(owner: Engine) -> None:
    from sqlalchemy.orm import Session

    with Session(owner) as s, s.begin():
        for i in range(3):
            append_audit(
                s,
                action="policy.deny",
                target_type="task",
                target_id=f"task-imm-audit-{i}",
                result="DENY",
                actor_label="acct-imm-actor",
                correlation_id="corr-imm",
                workspace_id=WS,
                actor_account_id=ACTOR,
                metadata={"reason": "DEFAULT_DENY", "token": "must-not-be-stored"},
            )
        assert verify_chain(s, AUDIT_CHAIN) == []
        assert record_anchor(s, AUDIT_CHAIN, dt.date(2026, 1, 1)) is not None
        assert verify_anchors(s, AUDIT_CHAIN) == []
        stored = s.execute(
            text("SELECT redacted_metadata FROM audit_events ORDER BY id DESC LIMIT 1")
        ).scalar()
        assert stored["token"] == "<redacted>" and "must-not-be-stored" not in json.dumps(stored)


def test_verification_revisions_chain_and_tamper_detection(owner: Engine) -> None:
    from sqlalchemy.orm import Session

    _add_revision(owner, 1)
    _add_revision(owner, 2)
    with Session(owner) as s, s.begin():
        assert verify_chain(s, VERIFICATION_CHAIN) == []
        record_anchor(s, VERIFICATION_CHAIN, dt.date(2026, 1, 2))
        assert verify_anchors(s, VERIFICATION_CHAIN) == []
    # a superuser bypassing the trigger (the only way to change bytes) is still detected
    with owner.begin() as c:
        c.execute(
            text(
                "ALTER TABLE verification_revisions DISABLE TRIGGER "
                "trg_verification_revisions_immutable"
            )
        )
        c.execute(
            text(
                "UPDATE verification_revisions SET result = 'PASSED' "
                "WHERE verification_id = 'vr-imm-1' AND revision = 1"
            )
        )
        c.execute(
            text(
                "ALTER TABLE verification_revisions ENABLE TRIGGER "
                "trg_verification_revisions_immutable"
            )
        )
    with Session(owner) as s:
        problems = verify_chain(s, VERIFICATION_CHAIN)
        assert any("tampered" in p for p in problems)
        assert any("recomputed chain differs" in p for p in verify_anchors(s, VERIFICATION_CHAIN))
    with owner.begin() as c:  # restore so later tests see an intact chain
        c.execute(
            text(
                "ALTER TABLE verification_revisions DISABLE TRIGGER "
                "trg_verification_revisions_immutable"
            )
        )
        c.execute(
            text(
                "UPDATE verification_revisions SET result = 'FAILED' "
                "WHERE verification_id = 'vr-imm-1' AND revision = 1"
            )
        )
        c.execute(
            text(
                "ALTER TABLE verification_revisions ENABLE TRIGGER "
                "trg_verification_revisions_immutable"
            )
        )
    with Session(owner) as s:
        assert (
            verify_chain(s, VERIFICATION_CHAIN) == []
            and verify_anchors(s, VERIFICATION_CHAIN) == []
        )


def test_audit_chain_tamper_detected(owner: Engine) -> None:
    from sqlalchemy.orm import Session

    with owner.begin() as c:
        c.execute(text("ALTER TABLE audit_events DISABLE TRIGGER trg_audit_events_immutable"))
        c.execute(
            text("UPDATE audit_events SET result = 'ALLOW' WHERE target_id = 'task-imm-audit-1'")
        )
        c.execute(text("ALTER TABLE audit_events ENABLE TRIGGER trg_audit_events_immutable"))
    with Session(owner) as s:
        assert any("tampered" in p for p in verify_chain(s, AUDIT_CHAIN))
        assert verify_anchors(s, AUDIT_CHAIN)
    with owner.begin() as c:
        c.execute(text("ALTER TABLE audit_events DISABLE TRIGGER trg_audit_events_immutable"))
        c.execute(
            text("UPDATE audit_events SET result = 'DENY' WHERE target_id = 'task-imm-audit-1'")
        )
        c.execute(text("ALTER TABLE audit_events ENABLE TRIGGER trg_audit_events_immutable"))
