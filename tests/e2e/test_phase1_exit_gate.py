"""Phase 1 Exit Gate (development plan §14): Task create → delegate → Approval → submit → Artifact
→ draft → independent verify → finalize → complete, over REST only, without Agents or Mattermost,
with the real Policy Engine (seeded roles) and credential-derived principals."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.config import Settings
from server.db.engine import make_engine
from server.identity.principals import token_hash
from server.main import create_app
from server.policy.repository import PostgresPolicyRepository

pytestmark = pytest.mark.db

WS = uuid.uuid4()
CHANNEL = uuid.uuid4()
CREATOR, AGENT, VERIFIER, APPROVER = (uuid.uuid4() for _ in range(4))
TOKENS = {
    "creator": "svc-eg-creator",
    "agent": "svc-eg-agent",
    "verifier": "svc-eg-verifier",
    "approver": "svc-eg-approver",
}


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    now = dt.datetime(2026, 3, 1, tzinfo=dt.UTC)
    with Session(eng) as s, s.begin():
        s.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-eg', 'eg')"),
            {"i": WS},
        )
        for acc, name, typ, tok in (
            (CREATOR, "acct-eg-creator", "human", TOKENS["creator"]),
            (AGENT, "acct-eg-agent", "agent", TOKENS["agent"]),
            (VERIFIER, "acct-eg-verifier", "human", TOKENS["verifier"]),
            (APPROVER, "acct-eg-approver", "human", TOKENS["approver"]),
        ):
            s.execute(
                text(
                    "INSERT INTO accounts "
                    "(id, account_id, workspace_id, account_type, display_name) "
                    "VALUES (:i, :a, :w, :t, :a)"
                ),
                {"i": acc, "a": name, "w": WS, "t": typ},
            )
            s.execute(
                text(
                    "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) "
                    "VALUES (:i, :a, :f, :h)"
                ),
                {"i": uuid.uuid4(), "a": acc, "f": f"sha256:{name}", "h": token_hash(tok)},
            )
        s.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name) "
                "VALUES (:i, 'chan-eg', :w, 'work', 'eg')"
            ),
            {"i": CHANNEL, "w": WS},
        )
        for acc in (CREATOR, AGENT, VERIFIER, APPROVER):
            s.execute(
                text("INSERT INTO channel_members (channel_id, account_id) VALUES (:c, :a)"),
                {"c": CHANNEL, "a": acc},
            )
        repo = PostgresPolicyRepository()
        roles = {
            "eg-member": (
                [
                    "task.create",
                    "task.read",
                    "task.delegate",
                    "task.complete",
                    "task.cancel",
                    "approval.request",
                    "artifact.read",
                    "document.read",
                ],
                {},
            ),
            "eg-worker": (
                [
                    "task.read",
                    "task.accept",
                    "task.progress",
                    "task.submit",
                    "artifact.write",
                    "artifact.read",
                    "work.poll",
                ],
                {},
            ),
            "eg-verifier": (
                [
                    "task.read",
                    "verification.assign",
                    "verification.submit",
                    "verification.read",
                    "artifact.read",
                    "document.read",
                ],
                {},
            ),
            "eg-approver": (
                ["task.read", "approval.decide", "approval.read"],
                {"max_risk": "HIGH"},
            ),
        }
        for role_id, (perms, constraints) in roles.items():
            repo.create_role(s, WS, role_id, role_id)
            repo.commit_role_version(s, role_id, perms, [], constraints, CREATOR)
        for acc, role_id in (
            (CREATOR, "eg-member"),
            (AGENT, "eg-worker"),
            (VERIFIER, "eg-verifier"),
            (APPROVER, "eg-approver"),
        ):
            repo.assign_role(s, acc, role_id, CREATOR, now)
    yield eng
    eng.dispose()


def _h(who: str, key: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[who]}", "Idempotency-Key": key, **extra}


def test_phase1_exit_gate_full_chain(database_url: str, engine: Engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    import os

    os.environ["AGENT_COLAB_ARTIFACT_ROOT"] = str(tmp_path / "artifacts")
    os.environ["AGENT_COLAB_DOCUMENT_ROOT"] = str(tmp_path / "documents")
    app = create_app(Settings(database_url=database_url, base_url="http://test"))
    with TestClient(app) as c:
        # 1. create with acceptance criteria (creator)
        r = c.post(
            "/api/v1/tasks",
            json={
                "title": "Exit gate task",
                "channel_id": str(CHANNEL),
                "domain": "research",
                "risk": "LOW",
                "criteria": [
                    {
                        "statement": "report artifact attached",
                        "check_type": "artifact_hash",
                        "required": True,
                    }
                ],
            },
            headers=_h("creator", "eg-create"),
        )
        assert r.status_code == 201, r.text
        task_id = r.json()["resource_id"]
        crit_id = c.get(f"/api/v1/tasks/{task_id}", headers=_h("creator", "x")).json()
        # 2. delegate to the agent, agent accepts and starts
        assert (
            c.post(
                f"/api/v1/tasks/{task_id}/delegate",
                json={"assignee_account_id": "acct-eg-agent"},
                headers=_h("creator", "eg-del"),
            ).status_code
            == 200
        )
        assert (
            c.post(f"/api/v1/tasks/{task_id}/accept", headers=_h("agent", "eg-acc")).status_code
            == 200
        )
        assert (
            c.post(f"/api/v1/tasks/{task_id}/start", headers=_h("agent", "eg-start")).status_code
            == 200
        )
        assert (
            c.post(
                f"/api/v1/tasks/{task_id}/progress",
                json={"summary": "half way"},
                headers=_h("agent", "eg-prog"),
            ).status_code
            == 200
        )
        # 3. Approval for a HIGH action: requester cannot approve; approver decides with re-auth
        ra = c.post(
            "/api/v1/approvals",
            json={"subject_type": "task", "subject_id": task_id, "action": "external_send"},
            headers=_h("creator", "eg-apr"),
        )
        assert ra.status_code == 201, ra.text
        approval_id = ra.json()["resource_id"]
        self_try = c.post(
            f"/api/v1/approvals/{approval_id}/decide",
            json={"decision": "APPROVE", "reauth_verified": True},
            headers=_h("creator", "eg-self"),
        )
        assert self_try.status_code == 404 and self_try.json()["code"] == "SELF_APPROVAL_FORBIDDEN"
        no_reauth = c.post(
            f"/api/v1/approvals/{approval_id}/decide",
            json={"decision": "APPROVE"},
            headers=_h("approver", "eg-noreauth"),
        )
        assert no_reauth.json()["code"] == "REAUTH_REQUIRED"
        ok = c.post(
            f"/api/v1/approvals/{approval_id}/decide",
            json={"decision": "APPROVE", "reauth_verified": True},
            headers=_h("approver", "eg-approve"),
        )
        assert ok.status_code == 200, ok.text
        assert (
            c.get(f"/api/v1/approvals/{approval_id}", headers=_h("creator", "x")).json()["status"]
            == "APPROVED"
        )
        # 4. submit without evidence for the required criterion is rejected, then with evidence
        with Session(engine) as s:
            crit_id = s.execute(
                text("SELECT criteria_id FROM task_acceptance_criteria WHERE task_id = :t"),
                {"t": task_id},
            ).scalar_one()
        bad = c.post(
            f"/api/v1/tasks/{task_id}/submit",
            json={"evidence_refs": [], "criteria_revision": 1},
            headers=_h("agent", "eg-sub-bad"),
        )
        assert bad.status_code == 422 and bad.json()["code"] == "EVIDENCE_REQUIRED"
        sub = c.post(
            f"/api/v1/tasks/{task_id}/submit",
            json={"evidence_refs": [f"{crit_id}:art-report-sha"], "criteria_revision": 1},
            headers=_h("agent", "eg-sub"),
        )
        assert sub.status_code == 200, sub.text
        # 5. the pre-verification draft exists automatically, without a verdict
        with Session(engine) as s:
            versions = s.execute(
                text(
                    "SELECT v.version, v.status FROM document_versions v "
                    "JOIN documents d ON d.document_id = v.document_id "
                    "WHERE d.source_id = :t ORDER BY v.version"
                ),
                {"t": task_id},
            ).all()
        assert [tuple(v) for v in versions] == [(1, "DRAFT_PRE_VERIFICATION")]
        # completion before verification is impossible
        pre = c.post(
            f"/api/v1/tasks/{task_id}/complete",
            json={"document_id": "doc-any"},
            headers=_h("creator", "eg-comp-0"),
        )
        assert pre.status_code == 409 and pre.json()["code"] in (
            "VERIFICATION_REQUIRED",
            "TASK_TRANSITION_INVALID",
        )
        # 6. independent verification: the implementing agent cannot verify itself
        run_body = {
            "target_type": "task",
            "target_id": task_id,
            "task_id": task_id,
            "implementer_account_id": "acct-eg-agent",
            "verifier_account_id": "acct-eg-verifier",
            "implementer_credential_fingerprint": "sha256:acct-eg-agent",
            "verifier_credential_fingerprint": "sha256:acct-eg-verifier",
            "target_commit": "0" * 40,
            "identity_graph_version": "identity-v8-001",
            "effective_policy_hash": "sha256:policy",
        }
        vr = c.post("/api/v1/verification-runs", json=run_body, headers=_h("verifier", "eg-vr"))
        assert vr.status_code == 201, vr.text
        vid = (
            vr.json()["resource_id"] if "resource_id" in vr.json() else vr.json()["verification_id"]
        )
        for step, key in (("assign", "eg-vr-assign"), ("start", "eg-vr-start")):
            resp = c.post(f"/api/v1/verification-runs/{vid}/{step}", headers=_h("verifier", key))
            assert resp.status_code in (200, 201), resp.text
        report = {
            "result": "PASSED",
            "criteria_version": "v8.0",
            "tests": [{"id": "V-P1-EG", "result": "PASS", "evidence_ref": "art-report-sha"}],
            "findings": [],
            "residual_risks": [],
        }
        forged = c.post(
            f"/api/v1/verification-runs/{vid}/verdict",
            json={"result": "PASSED", "report": report},
            headers=_h("agent", "eg-forge"),
        )
        assert (
            forged.status_code in (404, 409)
            and forged.json()["code"] == "SELF_VERIFICATION_FORBIDDEN"
        )
        verdict = c.post(
            f"/api/v1/verification-runs/{vid}/verdict",
            json={"result": "PASSED", "report": report},
            headers=_h("verifier", "eg-verdict"),
        )
        assert verdict.status_code in (200, 201), verdict.text
        # 7. the FINALIZED version exists automatically; complete with its document id
        with Session(engine) as s:
            fin = s.execute(
                text(
                    "SELECT v.document_id, v.version, v.status FROM document_versions v "
                    "JOIN documents d ON d.document_id = v.document_id "
                    "WHERE d.source_id = :t ORDER BY v.version DESC LIMIT 1"
                ),
                {"t": task_id},
            ).first()
        assert fin is not None and fin[2] == "FINALIZED"
        wrong = c.post(
            f"/api/v1/tasks/{task_id}/complete",
            json={"document_id": "doc-wrong"},
            headers=_h("creator", "eg-comp-1"),
        )
        assert wrong.json()["code"] == "COMPLETION_PREREQUISITE_MISSING"
        done = c.post(
            f"/api/v1/tasks/{task_id}/complete",
            json={"document_id": fin[0]},
            headers=_h("creator", "eg-comp-2"),
        )
        assert done.status_code == 200, done.text
        final = c.get(f"/api/v1/tasks/{task_id}", headers=_h("creator", "x")).json()
        assert final["status"] == "COMPLETED" and final["verification_status"] == "PASSED"
        # terminal: any further write is rejected with zero Events
        with Session(engine) as s:
            n_before = s.execute(
                text("SELECT count(*) FROM events WHERE task_id = :t"), {"t": task_id}
            ).scalar_one()
        again = c.post(
            f"/api/v1/tasks/{task_id}/progress",
            json={"summary": "late"},
            headers=_h("agent", "eg-late"),
        )
        assert again.status_code == 409 and again.json()["code"] == "TASK_TERMINAL"
        with Session(engine) as s:
            assert (
                s.execute(
                    text("SELECT count(*) FROM events WHERE task_id = :t"), {"t": task_id}
                ).scalar_one()
                == n_before
            )
