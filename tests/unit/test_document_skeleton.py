"""P1-10 unit tests: exact headings, byte-reproducibility, UNAVAILABLE placeholders, manifest."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from server.documents import templates
from server.documents.builder import (
    UNAVAILABLE_NO_USAGE,
    DocumentBuildError,
    SourceFreeze,
    TaskSources,
    build_skeleton,
    document_id_for_task,
)
from server.documents.store import DocumentStore, DocumentStoreError
from server.domain.task import fold

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "documents" / "skeleton-sources.json").read_text(
        encoding="utf-8"
    )
)
SCHEMA = json.loads(
    (ROOT / "schemas" / "documents" / "document-manifest.v1.schema.json").read_text(
        encoding="utf-8"
    )
)
EXPECTED_HEADINGS = [
    "Purpose and Scope",
    "Participants and Roles",
    "Inputs and Resources Used",
    "Process and Key Events",
    "Discussion, Alternatives, Decisions and Rationale",
    "Results and Artifacts",
    "Verification Method and Results",
    "Shortcomings, Risks and Open Questions",
    "Follow-up Work",
    "Provenance",
]


def _sources(**over: Any) -> TaskSources:
    f = FIXTURE
    src = TaskSources(
        task_id=f["task_id"],
        workspace_id=f["workspace_id"],
        freeze=SourceFreeze(f["task_id"], 15),
        events=[dict(e) for e in f["events"]],
        criteria=list(f["criteria"]),
        artifacts=list(f["artifacts"]),
        accounts=dict(f["accounts"]),
        sensitive_keys=dict(f["sensitive_keys"]),
    )
    src.state = fold(src.task_id, src.events)
    for k, v in over.items():
        setattr(src, k, v)
    return src


def test_template_renders_exact_headings_in_order() -> None:
    md = templates.render("T", {})
    assert templates.headings_of(md) == EXPECTED_HEADINGS
    assert md.count(templates.EMPTY_MARKER) == len(EXPECTED_HEADINGS)
    with pytest.raises(ValueError):
        templates.render("T", {"bogus": ["x"]})


def test_draft_skeleton_is_byte_reproducible_and_has_no_verdict() -> None:
    doc_id = document_id_for_task(FIXTURE["task_id"])
    a = build_skeleton(_sources(), "DRAFT_PRE_VERIFICATION", document_id=doc_id, version=1)
    b = build_skeleton(_sources(), "DRAFT_PRE_VERIFICATION", document_id=doc_id, version=1)
    assert a.markdown == b.markdown and a.sha256 == b.sha256 and a.manifest == b.manifest
    assert templates.headings_of(a.markdown) == EXPECTED_HEADINGS
    assert "Result: PENDING (pre-verification draft)" in a.markdown
    assert "PASSED" not in a.markdown.split("## Verification Method and Results")[1].split("##")[0]
    assert a.manifest["verification"] is None
    assert a.manifest["status"] == "DRAFT_PRE_VERIFICATION"
    assert a.manifest["body_sha256"] in a.markdown  # provenance carries the body checksum
    assert "[[evt:evt-1]]" in a.markdown and "[[art:art-0001]]" in a.markdown


def test_sensitive_events_are_never_rendered() -> None:
    doc_id = document_id_for_task(FIXTURE["task_id"])
    active = build_skeleton(_sources(), "DRAFT_PRE_VERIFICATION", document_id=doc_id, version=1)
    assert "[sensitive content: encrypted, not rendered]" in active.markdown
    shredded = build_skeleton(
        _sources(sensitive_keys={"dek://ws/task/task-fixture-0001": "destroyed"}),
        "DRAFT_PRE_VERIFICATION",
        document_id=doc_id,
        version=1,
    )
    assert "[sensitive content: redacted by crypto-shredding]" in shredded.markdown
    assert active.manifest["provenance"]["sensitive_event_ids"] == ["evt-5"]


def test_unavailable_placeholders_and_manifest_schema() -> None:
    doc_id = document_id_for_task(FIXTURE["task_id"])
    built = build_skeleton(_sources(), "DRAFT_PRE_VERIFICATION", document_id=doc_id, version=1)
    res = built.manifest["resources"]
    for key in ("agents", "models", "tools", "input_tokens", "cost_units"):
        assert res[key] == UNAVAILABLE_NO_USAGE
    assert res["artifacts"] == ["art-0001"]
    Draft202012Validator(SCHEMA).validate(built.manifest)
    usage = [
        {
            "agent_id": "agent-1",
            "model": "m",
            "input_tokens": 10,
            "output_tokens": 5,
            "tool_calls": 1,
            "wall_ms": 200,
            "cost_units": 42,
            "source": "computed",
            "unavailable_reason": None,
        }
    ]
    with_usage = build_skeleton(
        _sources(usage=usage), "DRAFT_PRE_VERIFICATION", document_id=doc_id, version=1
    )
    assert with_usage.manifest["resources"]["cost_units"] == 42
    assert with_usage.manifest["resources"]["agents"] == ["agent-1"]
    Draft202012Validator(SCHEMA).validate(with_usage.manifest)


def test_finalized_requires_passed_and_attempt_requires_non_passed() -> None:
    doc_id = document_id_for_task(FIXTURE["task_id"])
    run = {
        "verification_id": "vr-1",
        "status": "FAILED",
        "current_revision": 1,
        "result": "FAILED",
        "implementer": "22222222-2222-4222-8222-222222222222",
        "verifier": "11111111-1111-4111-8111-111111111111",
        "criteria_version": "v8.0",
        "target_commit": "abc",
        "snapshot_hash": "c" * 64,
        "revisions": [
            {
                "revision_id": "vrr-1",
                "revision": 1,
                "result": "FAILED",
                "report": {
                    "result": "FAILED",
                    "tests": [{"id": "V-X", "result": "FAIL", "evidence_ref": "e"}],
                    "findings": [{"id": "F-1", "severity": "High", "summary": "broken"}],
                    "residual_risks": ["risk one"],
                },
                "report_sha256": "d" * 64,
                "content_hash": "e" * 64,
                "event_id": "evt-9",
            }
        ],
        "findings": [
            {"finding_id": "vr-1:1:F-1", "revision": 1, "severity": "High", "summary": "broken"}
        ],
    }
    src = _sources(verifications=[run])
    with pytest.raises(DocumentBuildError) as exc:
        build_skeleton(src, "FINALIZED", document_id=doc_id, version=2, verification_id="vr-1")
    assert exc.value.code == "VERIFICATION_NOT_PASSED"
    attempt = build_skeleton(
        src, "ATTEMPT_FINALIZED", document_id=doc_id, version=2, verification_id="vr-1"
    )
    assert "**FAILED**" in attempt.markdown and "Residual risk: risk one" in attempt.markdown
    assert "Finding vr-1:1:F-1 [High]: broken" in attempt.markdown
    assert attempt.manifest["verification"] == {
        "verification_id": "vr-1",
        "revision": 1,
        "result": "FAILED",
        "findings": 1,
        "residual_risks": 1,
    }
    with pytest.raises(DocumentBuildError):
        build_skeleton(src, "ATTEMPT_FINALIZED", document_id=doc_id, version=2)  # no verification
    with pytest.raises(DocumentBuildError):
        build_skeleton(src, "PUBLISHED", document_id=doc_id, version=2)


def test_store_is_write_once(tmp_path: Path) -> None:
    store = DocumentStore(tmp_path)
    stored = store.write_version("ws-1", "doc-0000000000000001", 1, "# x\n", {"a": 1})
    assert stored.storage_uri == "colab-doc://ws-1/doc-0000000000000001/v1"
    assert store.read_version("ws-1", "doc-0000000000000001", 1) == ("# x\n", {"a": 1})
    with pytest.raises(DocumentStoreError) as exc:
        store.write_version("ws-1", "doc-0000000000000001", 1, "# y\n", {})
    assert exc.value.code == "DOCUMENT_VERSION_EXISTS"
    with pytest.raises(DocumentStoreError):
        store.write_version("../x", "doc-0000000000000001", 2, "", {})
