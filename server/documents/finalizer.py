"""Documentation pipeline orchestrator (development plan §10.1).

``SOURCE_FREEZE → COLLECT → DRAFT_PRE_VERIFICATION → LINK_PROVENANCE → REDACT →
INDEPENDENT_VERIFY → FINALIZE_NEW_VERSION → HUMAN_REVIEW? → PUBLISH → ARCHIVE``

Tasks keep the Phase 1 two-stage lifecycle (:mod:`server.documents.lifecycle`): the
pre-verification draft never carries a verdict, ``FAILED``/``BLOCKED`` produce an immutable
``ATTEMPT_FINALIZED`` version, and only ``PASSED`` produces ``FINALIZED``, which the Task
completion gate requires. Brainstorms, Schedule Runs and Schedule periods have no VerificationRun
of their own, so they produce drafts that the publish review (P6-07) gates instead.

Every stage after the build is applied here: provenance rows with checksums, redaction counts, and
the optional narrative layer. Redaction itself happens inside the builder so the stored bytes and
their hash are already clean.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.documents import narrative as narr
from server.documents import provenance, redaction, sources
from server.documents.builder import BuiltDocument, DocumentBuildError
from server.documents.lifecycle import (
    DocumentActor,
    DocumentLifecycleError,
    DocumentVersion,
    _persist_version,
    accepted_narrative,
    draft_document,
    finalize_attempt,
    list_versions,
)
from server.documents.store import DocumentStore
from server.documents.templates import TEMPLATE_VERSION, render
from server.events.canonical import canonical_json
from server.events.store import AppendRequest, EventStore, EventStoreError

GENERATOR = "agent-colab.documents.finalizer/layer-1"
DOC_TYPE = {
    "task": "task",
    "brainstorm": "brainstorm",
    "schedule_run": "schedule_run",
    "schedule_period": "period",
}
UNAVAILABLE_NO_USAGE = "UNAVAILABLE_NO_USAGE_REPORTED"
UNAVAILABLE_NO_ARTIFACTS = "UNAVAILABLE_NO_ARTIFACTS"
UNAVAILABLE_NOT_REPORTED = "UNAVAILABLE_NOT_REPORTED"


@dataclass(frozen=True)
class PipelineResult:
    document_id: str
    version: int
    status: str
    sha256: str
    subject_type: str
    subject_id: str
    freeze_id: str
    provenance_refs: int
    unresolved: list[provenance.Unresolved]
    redactions: list[redaction.RedactionCount]
    narrative_status: str
    replayed: bool = False
    document_version: DocumentVersion | None = None  # the stored version, for bus results


# ---------------------------------------------------------------- skeleton for non-Task subjects


def _acct(src: sources.SubjectSources, account_uuid: str | None) -> str:
    if not account_uuid:
        return "unknown"
    return src.accounts.get(account_uuid, account_uuid)


def _resources(src: sources.SubjectSources) -> dict[str, Any]:
    """Every field carries a value or a standard ``UNAVAILABLE_<REASON>`` (V-P6-11)."""
    artifacts = [a["artifact_id"] for a in src.artifacts] or UNAVAILABLE_NO_ARTIFACTS
    if not src.usage:
        return {
            "agents": UNAVAILABLE_NO_USAGE,
            "models": UNAVAILABLE_NO_USAGE,
            "tools": UNAVAILABLE_NO_USAGE,
            "input_tokens": UNAVAILABLE_NO_USAGE,
            "output_tokens": UNAVAILABLE_NO_USAGE,
            "tool_calls": UNAVAILABLE_NO_USAGE,
            "wall_ms": UNAVAILABLE_NO_USAGE,
            "cost_units": UNAVAILABLE_NO_USAGE,
            "artifacts": artifacts,
            "sources": UNAVAILABLE_NO_USAGE,
        }
    return {
        "agents": sorted({str(u["agent_id"]) for u in src.usage if u["agent_id"]})
        or UNAVAILABLE_NOT_REPORTED,
        "models": sorted({str(u["model"]) for u in src.usage if u["model"]})
        or UNAVAILABLE_NOT_REPORTED,
        "tools": UNAVAILABLE_NOT_REPORTED,  # tool names are not part of usage_records (§7C)
        "input_tokens": sum(int(u["input_tokens"]) for u in src.usage),
        "output_tokens": sum(int(u["output_tokens"]) for u in src.usage),
        "tool_calls": sum(int(u["tool_calls"]) for u in src.usage),
        "wall_ms": sum(int(u["wall_ms"]) for u in src.usage),
        "cost_units": sum(int(u["cost_units"]) for u in src.usage),
        "artifacts": artifacts,
        "sources": sorted({str(u["source"]) for u in src.usage}),
    }


def _fmt(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "none"
    return str(value)


def _event_lines(src: sources.SubjectSources) -> list[str]:
    return [
        f"- {e['occurred_at']} `{e['type']}` [[evt:{e['event_id']}]] by "
        f"{_acct(src, e['actor_account_id'])}"
        for e in src.events
    ]


def _run_sections(src: sources.SubjectSources) -> dict[str, list[str]]:
    run = src.rows["run"]
    task = src.rows.get("task")
    attempts = src.rows.get("attempts", [])
    sections: dict[str, list[str]] = {
        "purpose": [
            f"- Schedule Run: `{run['run_id']}` of schedule `{run['schedule_id']}` "
            f"({run['schedule_name']})",
            f"- Kind: {run['run_kind']}; occurrence key: {run['occurrence_key'] or 'n/a'}",
            f"- Scheduled for: {run['scheduled_for']} (cron `{run['cron_expression']}` "
            f"in {run['timezone']}, version {run['version_no']})",
            f"- Terminal status: {run['status']}",
        ],
        "participants": [
            f"- Requested by: {_acct(src, run.get('requested_by'))}",
            f"- Pinned ScheduleVersion hash: {str(run['version_hash'])[:16]}…",
        ],
        "process": _event_lines(src)
        + [
            f"- Attempt {a['attempt_no']}: {a['result'] or 'no result'}"
            + (f" ({a['error_code']})" if a["error_code"] else "")
            for a in attempts
        ],
        "discussion": ["_Narrative layer not generated (development plan §10.4 layer 2)._"],
        "results": [],
        "verification": [],
        "shortcomings": [f"- {line}" for line in src.limitations],
        "followup": [],
    }
    if task:
        sections["results"].append(
            f"- Task `{task['task_id']}` — {task['title']} (status {task['status']}, "
            f"risk {task['risk']}, verification {task['verification_status'] or 'none'})"
        )
        sections["verification"].append(
            f"- Task verification status: {task['verification_status'] or 'none recorded'}"
        )
    else:
        sections["verification"].append("- No Task was created, so no verification applies.")
    for a in src.artifacts:
        sections["results"].append(
            f"- Artifact [[art:{a['artifact_id']}]] ({a['mime']}, {a['size']} bytes, "
            f"sha256 {a['sha256']}, {a['status']}, relation {a['relation']})"
        )
    if run["retry_of_run_id"]:
        sections["followup"].append(f"- This Run retries `{run['retry_of_run_id']}`.")
    if str(run["status"]) not in ("SUCCEEDED", "CANCELLED"):
        sections["followup"].append("- A follow-up Run or manual retry may be required.")
    return sections


def _period_sections(src: sources.SubjectSources) -> dict[str, list[str]]:
    rows = src.rows
    runs = rows["runs"]
    by_status: dict[str, int] = {}
    for r in runs:
        by_status[str(r["status"])] = by_status.get(str(r["status"]), 0) + 1
    sections: dict[str, list[str]] = {
        "purpose": [
            f"- Schedule: `{rows['schedule']['schedule_id']}` ({rows['schedule']['name']})",
            f"- Period: {rows['period']} from {rows['start'].isoformat()} to "
            f"{rows['end'].isoformat()}",
            f"- Runs in window: {len(runs)}",
        ],
        "participants": [f"- Schedule status at generation: {rows['schedule']['status']}"],
        "process": [
            f"- {r['scheduled_for']} [[run:{r['run_id']}]] {r['run_kind']} → {r['status']}"
            + (f" ({r['error_code']})" if r["error_code"] else "")
            + (f", Task `{r['task_id']}`" if r["task_id"] else "")
            for r in runs
        ],
        "discussion": ["_Narrative layer not generated (development plan §10.4 layer 2)._"],
        "results": [f"- {status}: {count} Run(s)" for status, count in sorted(by_status.items())],
        "verification": [
            "- Per-Run verification follows each Run's Task; this summary aggregates recorded "
            "outcomes only."
        ],
        "shortcomings": [f"- {line}" for line in src.limitations],
        "followup": [],
    }
    for a in src.artifacts:
        sections["results"].append(
            f"- Artifact [[art:{a['artifact_id']}]] ({a['mime']}, sha256 {a['sha256']})"
        )
    if by_status.get("FAILED") or by_status.get("TIMED_OUT"):
        sections["followup"].append("- Investigate the failed Runs listed above.")
    return sections


def _brainstorm_sections(src: sources.SubjectSources) -> dict[str, list[str]]:
    rows = src.rows
    session_row = rows["session"]
    turns, summaries, decisions = rows["turns"], rows["summaries"], rows["decisions"]
    by_type: dict[str, list[dict[str, Any]]] = {}
    for t in turns:
        by_type.setdefault(str(t["contribution_type"]), []).append(t)
    sections: dict[str, list[str]] = {
        "purpose": [
            f"- Brainstorm: `{session_row['brainstorm_id']}` — {session_row['topic']}",
            f"- Status: {session_row['status']}",
        ],
        "participants": [f"- Facilitator: {_acct(src, session_row.get('facilitator'))}"]
        + [
            f"- Participant: {_acct(src, str(a))} "
            f"({len([t for t in turns if t['account_id'] == a])} turn(s))"
            for a in sorted({str(t["account_id"]) for t in turns if t["account_id"]})
        ],
        "inputs": [],
        "process": _event_lines(src)
        + [
            f"- Turn {t['turn_no']} [{t['contribution_type']}] by {_acct(src, t['account_id'])}: "
            f"{t['body']}" + (f" [[evt:{t['event_id']}]]" if t["event_id"] else "")
            for t in turns
        ],
        # arguments and alternatives (V-P6-08)
        "discussion": [
            f"- {kind}: {len(items)} contribution(s)" for kind, items in sorted(by_type.items())
        ]
        + [
            f"  - [{t['contribution_type']}] {_acct(src, t['account_id'])}: {t['body']}"
            for t in turns
            if str(t["contribution_type"]) in ("CHALLENGE", "QUESTION", "GUIDANCE")
        ],
        "results": [],
        "verification": [
            "- Brainstorm sessions are not independently verified; the facilitator approves the "
            "summary and records Decisions."
        ],
        "shortcomings": [f"- {line}" for line in src.limitations],
        "followup": [],
    }
    for s in summaries:
        sections["results"].append(
            f"- Summary `{s['summary_id']}` ({'approved' if s['approved'] else 'draft'}): "
            f"{s['body']}"
        )
    for d in decisions:
        sections["results"].append(
            f"- Decision [[dec:{d['decision_id']}]]: {d['statement']} — rationale: {d['rationale']}"
        )
        sections["followup"].append(
            f"- Action items of Decision [[dec:{d['decision_id']}]] become Tasks with acceptance "
            "criteria (§7D)."
        )
    for a in src.artifacts:
        sections["results"].append(
            f"- Artifact [[art:{a['artifact_id']}]] ({a['mime']}, sha256 {a['sha256']})"
        )
    return sections


_SECTION_BUILDERS: dict[str, Callable[[sources.SubjectSources], dict[str, list[str]]]] = {
    "schedule_run": _run_sections,
    "schedule_period": _period_sections,
    "brainstorm": _brainstorm_sections,
}


def build_subject_skeleton(
    src: sources.SubjectSources,
    *,
    document_id: str,
    version: int,
    narrative_body: str | None = None,
) -> BuiltDocument:
    """Deterministic layer-1 skeleton for a non-Task subject (same freeze → same bytes)."""
    builder = _SECTION_BUILDERS.get(src.subject_type)
    if builder is None:
        raise DocumentBuildError("DOCUMENT_SUBJECT_UNSUPPORTED", src.subject_type)
    sections = builder(src)
    res = _resources(src)
    sections.setdefault("inputs", [])
    sections["inputs"] = [f"- {k}: {_fmt(v)}" for k, v in res.items()] + sections["inputs"]
    if narrative_body:
        sections["discussion"] = [narrative_body]
    event_ids = [e["event_id"] for e in src.events]
    artifact_ids = [a["artifact_id"] for a in src.artifacts]
    decision_ids = [str(d["decision_id"]) for d in src.rows.get("decisions", [])]
    run_ids = [str(r["run_id"]) for r in src.rows.get("runs", [])]
    if src.subject_type == "schedule_run":
        run_ids = [src.subject_id]
    provenance_lines = [
        f"- Source: {src.subject_type} `{src.subject_id}` (workspace {src.workspace_id})",
        # the freeze is identified by the hash of its source set, not by the attempt's id or
        # timestamp, so rebuilding the same sources yields byte-identical output
        f"- Source freeze manifest hash: {src.freeze.manifest_hash}",
        "- Event IDs: " + (", ".join(f"[[evt:{i}]]" for i in event_ids) if event_ids else "none"),
        "- Artifact IDs: " + (", ".join(f"[[art:{i}]]" for i in artifact_ids) or "none"),
        "- Decision IDs: " + (", ".join(f"[[dec:{i}]]" for i in decision_ids) or "none"),
        "- Schedule Run IDs: " + (", ".join(f"[[run:{i}]]" for i in run_ids) or "none"),
        f"- Generator: {GENERATOR}; template {TEMPLATE_VERSION}; document `{document_id}` "
        f"version {version} (DRAFT_PRE_VERIFICATION)",
    ]
    body_without_checksum, _ = redaction.redact(
        render(src.title, {**sections, "provenance": provenance_lines})
    )
    body_sha = hashlib.sha256(body_without_checksum.encode("utf-8")).hexdigest()
    provenance_lines.append(f"- Body SHA-256 (all sections above this line): {body_sha}")
    markdown, counts = redaction.redact(
        render(src.title, {**sections, "provenance": provenance_lines})
    )
    sha = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    manifest: dict[str, Any] = {
        "document_id": document_id,
        "version": version,
        "status": "DRAFT_PRE_VERIFICATION",
        "doc_type": DOC_TYPE[src.subject_type],
        "source_type": src.subject_type,
        "source_id": src.subject_id,
        "source_freeze_id": src.freeze.freeze_id,
        "source_freeze_event_seq": src.freeze.up_to_recorded_seq,
        "source_manifest_hash": src.freeze.manifest_hash,
        "template_version": TEMPLATE_VERSION,
        "generator": GENERATOR,
        "sha256": sha,
        "body_sha256": body_sha,
        "provenance": {
            "event_ids": event_ids,
            "artifact_ids": artifact_ids,
            "decision_ids": decision_ids,
            "schedule_run_ids": run_ids,
            "verification_ids": [],
            "message_ids": [],
        },
        "verification": None,
        "resources": res,
        "limitations": list(src.limitations),
        "redactions": redaction.as_manifest(counts),
    }
    canonical_json(manifest)
    return BuiltDocument(markdown, manifest, body_sha, sha)


# ---------------------------------------------------------------- persistence


def _ensure_document(
    session: Session, workspace_id: str, src: sources.SubjectSources, document_id: str
) -> int:
    row = session.execute(
        text("SELECT current_version FROM documents WHERE document_id = :d FOR UPDATE"),
        {"d": document_id},
    ).first()
    if row is None:
        session.execute(
            text(
                "INSERT INTO documents (id, document_id, workspace_id, doc_type, source_type, "
                "source_id, current_version, status) VALUES (:id, :d, CAST(:ws AS uuid), :dt, "
                ":st, :si, 0, 'DRAFT_PRE_VERIFICATION')"
            ),
            {
                "id": uuid.uuid4(),
                "d": document_id,
                "ws": workspace_id,
                "dt": DOC_TYPE[src.subject_type],
                "st": src.subject_type,
                "si": src.subject_id,
            },
        )
        return 1
    return int(row[0]) + 1


def _after_version(
    session: Session,
    *,
    document_id: str,
    version: int,
    manifest: dict[str, Any],
    markdown: str,
    workspace_id: str,
    subject_type: str,
    subject_id: str,
    freeze_id: str,
    narrative_status: str,
) -> PipelineResult:
    """LINK_PROVENANCE + REDACT bookkeeping for a version that is already stored."""
    refs = provenance.from_manifest(manifest)
    for kind, ref_id in provenance.citations_in(markdown):
        if (kind, ref_id) not in refs:
            refs.append((kind, ref_id))
    resolved, missing = provenance.resolve(session, refs)
    provenance.record(session, document_id, version, resolved)
    counts = [
        redaction.RedactionCount(str(r["rule"]), int(r["count"]), str(r["sample_hash"]))
        for r in manifest.get("redactions", [])
    ]
    redaction.record(session, document_id, version, counts)
    unresolved = [
        provenance.Unresolved(k, i, "MISSING") for k, i in (r.split(":", 1) for r in missing)
    ]
    return PipelineResult(
        document_id=document_id,
        version=version,
        status=str(manifest.get("status", "DRAFT_PRE_VERIFICATION")),
        sha256=str(manifest["sha256"]),
        subject_type=subject_type,
        subject_id=subject_id,
        freeze_id=freeze_id,
        provenance_refs=len(resolved),
        unresolved=unresolved,
        redactions=counts,
        narrative_status=narrative_status,
    )


def _narrative_for(
    session: Session,
    built: BuiltDocument,
    *,
    document_id: str,
    version: int,
    subject_type: str,
    subject_id: str,
    workspace_id: str,
    provider: narr.NarrativeProvider | None,
    clock: Any,
) -> narr.NarrativeOutcome:
    request = narr.NarrativeRequest(
        document_id=document_id,
        version=version,
        subject_type=subject_type,
        subject_id=subject_id,
        skeleton_markdown=built.markdown,
        manifest=built.manifest,
        known_refs=provenance.from_manifest(built.manifest)
        + provenance.citations_in(built.markdown),
    )
    return narr.generate(
        session, request, workspace_id=workspace_id, provider=provider, clock=clock
    )


def draft_subject(
    session: Session,
    store: EventStore,
    storage: DocumentStore,
    src: sources.SubjectSources,
    *,
    actor: DocumentActor,
    now: dt.datetime,
    provider: narr.NarrativeProvider | None = None,
    clock: Any = None,
    policy_version: str = "policy-v1",
) -> PipelineResult:
    """Draft a Brainstorm / Schedule Run / Schedule period document (one version per freeze)."""
    document_id = sources.document_id_for(src.subject_type, src.subject_id)
    sources.record_freeze(session, src.freeze, document_id)
    existing = list_versions(session, document_id)
    if existing:
        last = existing[-1]
        # compare at the stored version number: the number itself is part of the rendered text
        previous = build_subject_skeleton(
            src,
            document_id=document_id,
            version=int(last["version"]),
            narrative_body=accepted_narrative(session, document_id, int(last["version"])),
        )
        if str(last["sha256"]) == previous.sha256:
            return PipelineResult(
                document_id=document_id,
                version=int(last["version"]),
                status=str(last["status"]),
                sha256=str(last["sha256"]),
                subject_type=src.subject_type,
                subject_id=src.subject_id,
                freeze_id=src.freeze.freeze_id,
                provenance_refs=len(provenance.stored(session, document_id, int(last["version"]))),
                unresolved=[],
                redactions=redaction.counts_for(session, document_id, int(last["version"])),
                narrative_status="UNCHANGED",
                replayed=True,
            )
    version = _ensure_document(session, src.workspace_id, src, document_id)
    probe = build_subject_skeleton(src, document_id=document_id, version=version)
    outcome = _narrative_for(
        session,
        probe,
        document_id=document_id,
        version=version,
        subject_type=src.subject_type,
        subject_id=src.subject_id,
        workspace_id=src.workspace_id,
        provider=provider,
        clock=clock,
    )
    built = (
        build_subject_skeleton(
            src, document_id=document_id, version=version, narrative_body=outcome.body
        )
        if outcome.accepted
        else probe
    )
    try:
        res = store.append(
            AppendRequest(
                workspace_id=src.workspace_id,
                aggregate_type="document",
                aggregate_id=document_id,
                type="DOCUMENT_DRAFTED",
                actor_account_id=actor.account_uuid,
                correlation_id=actor.correlation_id,
                idempotency_scope="document:draft",
                idempotency_key=actor.idempotency_key,
                payload={
                    "document_id": document_id,
                    "source_type": src.subject_type,
                    "source_id": src.subject_id,
                    "version": version,
                    "sha256": built.sha256,
                    "source_freeze_event_seq": src.freeze.up_to_recorded_seq,
                },
                policy_version=policy_version,
                expected_seq=version,
            )
        )
    except EventStoreError as exc:
        raise DocumentLifecycleError(exc.code, exc.detail) from exc
    _persist_version(
        session,
        storage,
        built,
        workspace_id=src.workspace_id,
        document_id=document_id,
        version=version,
        status="DRAFT_PRE_VERIFICATION",
        verification_id=None,
        verification_result=None,
        event_id=res.event_id,
        freeze_seq=src.freeze.up_to_recorded_seq,
    )
    return _after_version(
        session,
        document_id=document_id,
        version=version,
        manifest=built.manifest,
        markdown=built.markdown,
        workspace_id=src.workspace_id,
        subject_type=src.subject_type,
        subject_id=src.subject_id,
        freeze_id=src.freeze.freeze_id,
        narrative_status=outcome.status,
    )


# ---------------------------------------------------------------- Task pipeline


def _task_freeze(session: Session, task_id: str, version: DocumentVersion, now: dt.datetime) -> str:
    """Record the freeze ledger row for a Task version built by the Phase 1 lifecycle."""
    manifest = json.loads(
        session.execute(
            text(
                "SELECT manifest::text FROM document_versions WHERE document_id = :d "
                "AND version = :v"
            ),
            {"d": version.document_id, "v": version.version},
        ).scalar_one()
    )
    workspace_id = str(
        session.execute(
            text("SELECT workspace_id::text FROM documents WHERE document_id = :d"),
            {"d": version.document_id},
        ).scalar_one()
    )
    prov = manifest.get("provenance", {})
    source_manifest = {
        "subject": {"type": "task", "id": task_id},
        "up_to_recorded_seq": version.source_freeze_event_seq,
        "event_ids": prov.get("event_ids", []),
        "artifact_ids": prov.get("artifact_ids", []),
        "verification_ids": prov.get("verification_ids", []),
    }
    freeze = sources.Freeze(
        freeze_id="frz-" + uuid.uuid4().hex[:20],
        subject_type="task",
        subject_id=task_id,
        workspace_id=workspace_id,
        frozen_at=now,
        up_to_recorded_seq=version.source_freeze_event_seq,
        source_manifest=source_manifest,
        manifest_hash=provenance.manifest_hash(source_manifest),
    )
    sources.record_freeze(session, freeze, version.document_id)
    return freeze.freeze_id


def _task_pipeline_result(
    session: Session,
    task_id: str,
    version: DocumentVersion,
    now: dt.datetime,
    narrative_status: str,
) -> PipelineResult:
    row = session.execute(
        text(
            "SELECT manifest::text, workspace_id::text FROM document_versions v "
            "JOIN documents d ON d.document_id = v.document_id "
            "WHERE v.document_id = :d AND v.version = :v"
        ),
        {"d": version.document_id, "v": version.version},
    ).first()
    if row is None:
        raise DocumentLifecycleError("DOCUMENT_VERSION_MISSING", version.document_id)
    manifest = json.loads(str(row[0]))
    workspace_id = str(row[1])
    freeze_id = _task_freeze(session, task_id, version, now)
    storage = DocumentStore()
    try:
        markdown, _ = storage.read_version(workspace_id, version.document_id, version.version)
    except Exception:  # the store root may differ in this process; citations come from the manifest
        markdown = ""
    result = _after_version(
        session,
        document_id=version.document_id,
        version=version.version,
        manifest=manifest,
        markdown=markdown,
        workspace_id=workspace_id,
        subject_type="task",
        subject_id=task_id,
        freeze_id=freeze_id,
        narrative_status=narrative_status,
    )
    return replace(result, document_version=version, replayed=version.replayed)


def draft_task(
    session: Session,
    store: EventStore,
    storage: DocumentStore,
    *,
    task_id: str,
    actor: DocumentActor,
    now: dt.datetime,
    provider: narr.NarrativeProvider | None = None,
    clock: Any = None,
) -> PipelineResult:
    """Full pipeline for a Task draft: the Phase 1 lifecycle plus provenance, redaction, layer 2."""
    status = {"value": narr.STATUS_UNAVAILABLE}

    def hook(built: BuiltDocument, document_id: str, version: int, workspace_id: str) -> str | None:
        outcome = _narrative_for(
            session,
            built,
            document_id=document_id,
            version=version,
            subject_type="task",
            subject_id=task_id,
            workspace_id=workspace_id,
            provider=provider,
            clock=clock,
        )
        status["value"] = outcome.status
        return outcome.body if outcome.accepted else None

    version = draft_document(
        session, store, storage, task_id=task_id, actor=actor, narrative_hook=hook
    )
    return _task_pipeline_result(session, task_id, version, now, status["value"])


def finalize_task(
    session: Session,
    store: EventStore,
    storage: DocumentStore,
    *,
    task_id: str,
    verification_id: str,
    actor: DocumentActor,
    now: dt.datetime,
) -> PipelineResult:
    """FINALIZE_NEW_VERSION (PASSED) or the immutable ATTEMPT_FINALIZED version (FAILED/BLOCKED)."""
    version = finalize_attempt(
        session,
        store,
        storage,
        task_id=task_id,
        verification_id=verification_id,
        actor=actor,
    )
    return _task_pipeline_result(session, task_id, version, now, "SKELETON_ONLY")


# ---------------------------------------------------------------- reporting


def publishable_version(session: Session, document_id: str) -> dict[str, Any] | None:
    """The version the publisher (P6-06/P6-07) may publish.

    A Task document must be ``FINALIZED`` (its Verification passed); Brainstorm, Schedule Run and
    period documents have no VerificationRun and publish their latest draft after review.
    """
    row = (
        session.execute(
            text(
                "SELECT d.doc_type, v.version, v.status, v.sha256, v.storage_uri "
                "FROM documents d JOIN document_versions v ON v.document_id = d.document_id "
                "WHERE d.document_id = :d AND (d.doc_type = 'task') = (v.status = 'FINALIZED') "
                "ORDER BY v.version DESC LIMIT 1"
            ),
            {"d": document_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def generation_report(
    session: Session, workspace_id: str, *, subject_type: str | None = None
) -> dict[str, Any]:
    """Automatic-draft rate per subject type with the reason code of every failure (V-P6-20)."""
    params: dict[str, Any] = {"w": workspace_id, "st": subject_type}
    drafted = session.execute(
        text(
            "SELECT source_type, count(*) FROM documents WHERE workspace_id = CAST(:w AS uuid) "
            "AND (CAST(:st AS text) IS NULL OR source_type = CAST(:st AS text)) "
            "GROUP BY source_type"
        ),
        params,
    ).all()
    failures = session.execute(
        text(
            "SELECT subject_type, reason_code, count(*) FROM document_generation_failures "
            "WHERE workspace_id = CAST(:w AS uuid) "
            "AND (CAST(:st AS text) IS NULL OR subject_type = CAST(:st AS text)) "
            "GROUP BY subject_type, reason_code"
        ),
        params,
    ).all()
    return {
        "drafted": {str(r[0]): int(r[1]) for r in drafted},
        "failures": [
            {"subject_type": str(r[0]), "reason_code": str(r[1]), "count": int(r[2])}
            for r in failures
        ],
    }


__all__ = [
    "DOC_TYPE",
    "GENERATOR",
    "PipelineResult",
    "build_subject_skeleton",
    "draft_subject",
    "draft_task",
    "finalize_task",
    "generation_report",
    "publishable_version",
]
