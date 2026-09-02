"""Layer-1 deterministic skeleton builder (development plan §10.4, spec §14.1).

Everything in the document is derived from a frozen set of sources (Events up to a recorded
sequence, plus the authority tables they reference). The same freeze always yields the same
bytes; nothing is fetched from an LLM or the wall clock. Sensitive Event content is never
decrypted: the document only notes that encrypted content exists (or was crypto-shredded).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.documents.templates import HEADINGS, TEMPLATE_VERSION, render
from server.domain.task import TaskState, fold
from server.events.canonical import canonical_json
from server.events.postgres_store import _COLUMNS, row_to_event

GENERATOR = "agent-colab.documents.builder/layer-1"
UNAVAILABLE_NO_USAGE = "UNAVAILABLE_NO_USAGE_REPORTED"
UNAVAILABLE_NOT_REPORTED = "UNAVAILABLE_NOT_REPORTED"
UNAVAILABLE_NO_ARTIFACTS = "UNAVAILABLE_NO_ARTIFACTS"
STAGES = ("DRAFT_PRE_VERIFICATION", "ATTEMPT_FINALIZED", "FINALIZED")


class DocumentBuildError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SourceFreeze:
    task_id: str
    up_to_recorded_seq: int


@dataclass
class TaskSources:
    task_id: str
    workspace_id: str
    freeze: SourceFreeze
    events: list[dict[str, Any]] = field(default_factory=list)  # every Event tagged with the task
    state: TaskState | None = None
    channel: dict[str, Any] | None = None
    assignments: list[dict[str, Any]] = field(default_factory=list)
    criteria: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    verifications: list[dict[str, Any]] = field(default_factory=list)  # runs with revisions
    usage: list[dict[str, Any]] = field(default_factory=list)
    accounts: dict[str, str] = field(default_factory=dict)  # uuid -> public account_id
    sensitive_keys: dict[str, str] = field(default_factory=dict)  # key_ref -> status


@dataclass(frozen=True)
class BuiltDocument:
    markdown: str
    manifest: dict[str, Any]
    body_sha256: str
    sha256: str


# ---------------------------------------------------------------- freeze + collection


def freeze_for_task(session: Session, task_id: str) -> SourceFreeze:
    """Freeze at the highest recorded_seq of any Event tagged with the Task (0 = none)."""
    seq = session.execute(
        text(
            "SELECT COALESCE(MAX(recorded_seq), 0) FROM events "
            "WHERE (task_id = :t OR (aggregate_type = 'task' AND aggregate_id = :t)) "
            "AND aggregate_type <> 'document'"
        ),
        {"t": task_id},
    ).scalar_one()
    return SourceFreeze(task_id, int(seq))


def collect_task_sources(session: Session, task_id: str, freeze: SourceFreeze) -> TaskSources:
    rows = (
        session.execute(
            text(
                f"SELECT {_COLUMNS} FROM events WHERE (task_id = :t OR "  # noqa: S608
                "(aggregate_type = 'task' AND aggregate_id = :t)) AND recorded_seq <= :seq "
                "ORDER BY recorded_seq"
            ),
            {"t": task_id, "seq": freeze.up_to_recorded_seq},
        )
        .mappings()
        .all()
    )
    events = [row_to_event(r) for r in rows]
    task_events = [
        e for e in events if e["aggregate_type"] == "task" and e["aggregate_id"] == task_id
    ]
    if not task_events:
        raise DocumentBuildError("TASK_NOT_FOUND", task_id)
    verification_ids = sorted(
        {
            str(e["payload"].get("verification_id"))
            for e in task_events
            if e["type"] == "TASK_VERIFICATION_STARTED" and e["payload"].get("verification_id")
        }
    )
    merged = task_events + [
        e
        for e in events
        if e["aggregate_type"] == "verification_run" and e["aggregate_id"] in verification_ids
    ]
    state = fold(task_id, merged)
    src = TaskSources(
        task_id=task_id,
        workspace_id=task_events[0]["workspace_id"],
        freeze=freeze,
        events=events,
        state=state,
    )

    if state.channel_id:
        ch = (
            session.execute(
                text(
                    "SELECT channel_id, channel_type, display_name, external_channel_id "
                    "FROM channels WHERE id = CAST(:c AS uuid)"
                ),
                {"c": state.channel_id},
            )
            .mappings()
            .first()
        )
        src.channel = dict(ch) if ch else None

    src.assignments = [
        dict(r)
        for r in session.execute(
            text(
                "SELECT revision, delegator_account_id::text AS delegator, "
                "assignee_account_id::text AS assignee, reason_code, event_id "
                "FROM task_assignments WHERE task_id = :t ORDER BY revision"
            ),
            {"t": task_id},
        ).mappings()
    ]
    src.criteria = [
        dict(r)
        for r in session.execute(
            text(
                "SELECT criteria_id, revision, statement, check_type, required "
                "FROM task_acceptance_criteria WHERE task_id = :t ORDER BY revision, criteria_id"
            ),
            {"t": task_id},
        ).mappings()
    ]
    src.artifacts = [
        dict(r)
        for r in session.execute(
            text(
                "SELECT a.artifact_id, a.mime, a.size, a.sha256, a.status, l.relation "
                "FROM artifact_links l JOIN artifacts a ON a.artifact_id = l.artifact_id "
                "WHERE l.subject_type = 'task' AND l.subject_id = :t "
                "ORDER BY a.artifact_id, l.relation"
            ),
            {"t": task_id},
        ).mappings()
    ]
    runs = session.execute(
        text(
            "SELECT verification_id, status, current_revision, result, "
            "implementer_account_id::text AS implementer, "
            "verifier_account_id::text AS verifier, "
            "criteria_version, target_commit, snapshot_hash FROM verification_runs "
            "WHERE target_type = 'task' AND target_id = :t ORDER BY verification_id"
        ),
        {"t": task_id},
    ).mappings()
    for run in runs:
        rd = dict(run)
        rd["revisions"] = [
            dict(rv)
            for rv in session.execute(
                text(
                    "SELECT revision_id, revision, result, report, report_sha256, content_hash, "
                    "event_id FROM verification_revisions WHERE verification_id = :v "
                    "ORDER BY revision"
                ),
                {"v": rd["verification_id"]},
            ).mappings()
        ]
        rd["findings"] = [
            dict(fr)
            for fr in session.execute(
                text(
                    "SELECT finding_id, revision, severity, summary FROM verification_findings "
                    "WHERE verification_id = :v ORDER BY revision, finding_id"
                ),
                {"v": rd["verification_id"]},
            ).mappings()
        ]
        src.verifications.append(rd)
    src.usage = [
        dict(r)
        for r in session.execute(
            text(
                "SELECT agent_id, model, input_tokens, output_tokens, tool_calls, wall_ms, "
                "cost_units, source, unavailable_reason FROM usage_records WHERE task_id = :t "
                "ORDER BY id"
            ),
            {"t": task_id},
        ).mappings()
    ]
    uuids = {e["actor_account_id"] for e in events}
    for a in src.assignments:
        uuids.update({a["delegator"], a["assignee"]})
    for v in src.verifications:
        uuids.update({v["implementer"], v["verifier"]})
    if uuids:
        for r in session.execute(
            text(
                "SELECT id::text, account_id, account_type FROM accounts "
                "WHERE id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": sorted(uuids)},
        ):
            src.accounts[str(r[0])] = f"{r[1]} ({r[2]})"
    key_refs = sorted(
        {e["sensitive_payload_key_ref"] for e in events if e.get("sensitive_payload_key_ref")}
    )
    if key_refs:
        for r in session.execute(
            text("SELECT key_ref, status FROM sensitive_keys WHERE key_ref = ANY(:k)"),
            {"k": key_refs},
        ):
            src.sensitive_keys[str(r[0])] = str(r[1])
    return src


# ---------------------------------------------------------------- skeleton


def _acct(src: TaskSources, account_uuid: str | None) -> str:
    if not account_uuid:
        return "unknown"
    return src.accounts.get(account_uuid, account_uuid)


def _event_line(src: TaskSources, e: dict[str, Any]) -> str:
    extra = ""
    ref = e.get("sensitive_payload_key_ref")
    if ref:
        status = src.sensitive_keys.get(ref, "unknown")
        extra = (
            " [sensitive content: redacted by crypto-shredding]"
            if status == "destroyed"
            else " [sensitive content: encrypted, not rendered]"
        )
    p = e["payload"]
    summary = ""
    if e["type"] == "TASK_PROGRESS_REPORTED":
        summary = f": {p.get('summary', '')}"
    elif e["type"] in ("TASK_DELEGATED", "TASK_REASSIGNED"):
        summary = (
            f": assignee {_acct(src, p.get('assignee_account_id'))} "
            f"(revision {p.get('assignment_revision')})"
        )
    elif e["type"] == "TASK_WAITING":
        summary = f": {p.get('reason_code', '')}"
    elif e["type"].startswith("VERIFICATION_"):
        summary = f": {e['aggregate_id']} revision {p.get('revision')}"
    actor = _acct(src, e["actor_account_id"])
    return f"- {e['occurred_at']} `{e['type']}` [[evt:{e['event_id']}]] by {actor}{summary}{extra}"


def _resources(src: TaskSources) -> dict[str, Any]:
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
            "artifacts": [a["artifact_id"] for a in src.artifacts] or UNAVAILABLE_NO_ARTIFACTS,
            "sources": UNAVAILABLE_NO_USAGE,
        }
    agents = sorted({str(u["agent_id"]) for u in src.usage if u["agent_id"]})
    models = sorted({str(u["model"]) for u in src.usage if u["model"]})
    return {
        "agents": agents or UNAVAILABLE_NOT_REPORTED,
        "models": models or UNAVAILABLE_NOT_REPORTED,
        "tools": UNAVAILABLE_NOT_REPORTED,  # tool names are not part of usage_records (§7C)
        "input_tokens": sum(int(u["input_tokens"]) for u in src.usage),
        "output_tokens": sum(int(u["output_tokens"]) for u in src.usage),
        "tool_calls": sum(int(u["tool_calls"]) for u in src.usage),
        "wall_ms": sum(int(u["wall_ms"]) for u in src.usage),
        "cost_units": sum(int(u["cost_units"]) for u in src.usage),
        "artifacts": [a["artifact_id"] for a in src.artifacts] or UNAVAILABLE_NO_ARTIFACTS,
        "sources": sorted({str(u["source"]) for u in src.usage}),
    }


def _fmt(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "none"
    return str(value)


def _select_run(src: TaskSources, verification_id: str | None) -> dict[str, Any] | None:
    if verification_id is None:
        return None
    for run in src.verifications:
        if run["verification_id"] == verification_id:
            return run
    raise DocumentBuildError("VERIFICATION_NOT_FOUND", verification_id)


def build_skeleton(
    src: TaskSources,
    stage: str,
    *,
    document_id: str,
    version: int,
    verification_id: str | None = None,
) -> BuiltDocument:
    """Render the skeleton for a stage; PASSED verdicts may only appear in FINALIZED versions."""
    if stage not in STAGES:
        raise DocumentBuildError("DOCUMENT_STAGE_INVALID", stage)
    state = src.state
    assert state is not None
    run = _select_run(src, verification_id)
    if stage != "DRAFT_PRE_VERIFICATION":
        if run is None or not run["revisions"]:
            raise DocumentBuildError("VERIFICATION_NOT_TERMINAL", verification_id or "-")
        latest = run["revisions"][-1]
        if stage == "FINALIZED" and latest["result"] != "PASSED":
            raise DocumentBuildError(
                "VERIFICATION_NOT_PASSED", f"{verification_id}: {latest['result']}"
            )
        if stage == "ATTEMPT_FINALIZED" and latest["result"] == "PASSED":
            raise DocumentBuildError(
                "DOCUMENT_STAGE_INVALID", "PASSED verdicts finalize, not attempt-finalize"
            )
    else:
        latest = None

    task_events = [
        e for e in src.events if e["aggregate_type"] == "task" and e["aggregate_id"] == src.task_id
    ]
    sections: dict[str, list[str]] = {}
    sections["purpose"] = [
        f"- Task: `{src.task_id}` — {state.title}",
        f"- Domain: {state.domain or 'unspecified'}; risk: {state.risk}",
        f"- Root Task: `{state.root_task_id or src.task_id}`"
        + (f"; parent Task: `{state.parent_task_id}`" if state.parent_task_id else ""),
        f"- Status at source freeze (recorded_seq {src.freeze.up_to_recorded_seq}): "
        f"{state.status.value}",
    ]
    participants: list[str] = []
    creator = task_events[0]["actor_account_id"] if task_events else None
    participants.append(f"- Creator: {_acct(src, creator)}")
    for a in src.assignments:
        participants.append(
            f"- Assignment revision {a['revision']}: delegator {_acct(src, a['delegator'])} → "
            f"assignee {_acct(src, a['assignee'])} ({a['reason_code']})"
        )
    for v in src.verifications:
        participants.append(
            f"- Verification `{v['verification_id']}`: implementer {_acct(src, v['implementer'])}, "
            f"verifier {_acct(src, v['verifier'])} (independent identities, "
            f"snapshot {v['snapshot_hash'][:16]}…)"
        )
    sections["participants"] = participants

    res = _resources(src)
    sections["inputs"] = [f"- {k}: {_fmt(v)}" for k, v in res.items()]
    if src.criteria:
        sections["inputs"].append("- Acceptance criteria (latest revision):")
        latest_rev = max(c["revision"] for c in src.criteria)
        sections["inputs"] += [
            f"  - `{c['criteria_id']}` ({c['check_type']}, "
            f"{'required' if c['required'] else 'optional'}): {c['statement']}"
            for c in src.criteria
            if c["revision"] == latest_rev
        ]

    sections["process"] = [_event_line(src, e) for e in src.events]
    sections["discussion"] = [
        "_Narrative layer not generated (development plan §10.4 layer 2, Phase 6)._"
    ]

    results: list[str] = []
    submissions = [e for e in task_events if e["type"] == "IMPLEMENTATION_SUBMITTED"]
    for e in submissions:
        refs = e["payload"].get("evidence_refs", [])
        results.append(
            f"- Implementation submitted [[evt:{e['event_id']}]] with {len(refs)} "
            f"evidence ref(s): {_fmt(refs)}"
        )
    for a in src.artifacts:
        results.append(
            f"- Artifact [[art:{a['artifact_id']}]] ({a['mime']}, {a['size']} bytes, "
            f"sha256 {a['sha256']}, "
            f"{a['status']}, relation {a['relation']})"
        )
    sections["results"] = results

    verification: list[str] = []
    if src.criteria:
        verification.append(
            "- Method: acceptance criteria of the Task (see Inputs) are the verification "
            "baseline (§7D)."
        )
    if run is None:
        active = state.active_verification_id
        assigned = f"`{active}`" if active else "none yet"
        verification.append(f"- Verifier assigned: {assigned}")
        verification.append("- Result: PENDING (pre-verification draft)")
    else:
        assert latest is not None
        report = (
            latest["report"] if isinstance(latest["report"], dict) else json.loads(latest["report"])
        )
        verification.append(
            f"- Verification [[vr:{run['verification_id']}]] revision {latest['revision']}: "
            f"**{latest['result']}** (report sha256 {latest['report_sha256']}, "
            f"revision hash {latest['content_hash'][:16]}…)"
        )
        for t in report.get("tests", []):
            verification.append(
                f"  - {t.get('id')}: {t.get('result')} ({t.get('evidence_ref', '-')})"
            )
        for f in [f for f in run["findings"] if f["revision"] == latest["revision"]]:
            verification.append(f"  - Finding {f['finding_id']} [{f['severity']}]: {f['summary']}")
        if len(run["revisions"]) > 1:
            verification.append(
                "- Earlier attempts: "
                + ", ".join(
                    f"revision {r['revision']} {r['result']}" for r in run["revisions"][:-1]
                )
            )
    sections["verification"] = verification

    shortcomings: list[str] = []
    if latest is not None:
        report = (
            latest["report"] if isinstance(latest["report"], dict) else json.loads(latest["report"])
        )
        risks = report.get("residual_risks", [])
        shortcomings += [f"- Residual risk: {r}" for r in risks] or [
            "- Residual risks: none recorded by the verifier"
        ]
        if latest["result"] != "PASSED":
            shortcomings.append(
                f"- Verification {latest['result']}: the Task is not complete; "
                "a new revision is required."
            )
    unavailable = [k for k, v in res.items() if isinstance(v, str) and v.startswith("UNAVAILABLE_")]
    if unavailable:
        shortcomings.append(f"- Resource data unavailable for: {', '.join(unavailable)}")
    if any(e.get("sensitive_payload_key_ref") for e in src.events):
        shortcomings.append(
            "- Some Events carry encrypted sensitive content that is intentionally not rendered."
        )
    sections["shortcomings"] = shortcomings

    followup: list[str] = []
    if state.status.value not in ("COMPLETED", "CANCELLED"):
        followup.append(
            f"- Task `{src.task_id}` remains {state.status.value}; completion requires a "
            "PASSED verification and a FINALIZED document."
        )
    sections["followup"] = followup

    event_ids = [e["event_id"] for e in src.events]
    provenance_lines = [
        f"- Source: task `{src.task_id}` (workspace {src.workspace_id}); channel: "
        + (
            f"`{src.channel['channel_id']}` ({src.channel['channel_type']}, "
            f"external {src.channel.get('external_channel_id') or 'n/a'})"
            if src.channel
            else "n/a"
        ),
        f"- Source freeze: recorded_seq ≤ {src.freeze.up_to_recorded_seq}; "
        f"Events: {len(event_ids)}",
        "- Event IDs: " + (", ".join(f"[[evt:{i}]]" for i in event_ids) if event_ids else "none"),
        "- Artifact IDs: "
        + (", ".join(f"[[art:{a['artifact_id']}]]" for a in src.artifacts) or "none"),
        "- Verification IDs: "
        + (", ".join(f"[[vr:{v['verification_id']}]]" for v in src.verifications) or "none"),
        f"- Generator: {GENERATOR}; template {TEMPLATE_VERSION}; "
        f"document `{document_id}` version {version} ({stage})",
    ]
    # body checksum excludes the provenance checksum line itself
    body_without_checksum = render(
        state.title or src.task_id, {**sections, "provenance": provenance_lines}
    )
    body_sha = hashlib.sha256(body_without_checksum.encode("utf-8")).hexdigest()
    provenance_lines.append(f"- Body SHA-256 (all sections above this line): {body_sha}")
    markdown = render(state.title or src.task_id, {**sections, "provenance": provenance_lines})
    sha = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    verification_meta = None
    if run is not None and latest is not None:
        report = (
            latest["report"] if isinstance(latest["report"], dict) else json.loads(latest["report"])
        )
        verification_meta = {
            "verification_id": run["verification_id"],
            "revision": int(latest["revision"]),
            "result": str(latest["result"]),
            "findings": len([f for f in run["findings"] if f["revision"] == latest["revision"]]),
            "residual_risks": len(report.get("residual_risks", [])),
        }
    manifest: dict[str, Any] = {
        "document_id": document_id,
        "version": version,
        "status": stage,
        "doc_type": "task",
        "source_type": "task",
        "source_id": src.task_id,
        "source_freeze_event_seq": src.freeze.up_to_recorded_seq,
        "template_version": TEMPLATE_VERSION,
        "generator": GENERATOR,
        "sha256": sha,
        "body_sha256": body_sha,
        "provenance": {
            "task_id": src.task_id,
            "root_task_id": state.root_task_id,
            "channel": src.channel,
            "event_ids": event_ids,
            "artifact_ids": [a["artifact_id"] for a in src.artifacts],
            "verification_ids": [v["verification_id"] for v in src.verifications],
            "revision_ids": [r["revision_id"] for v in src.verifications for r in v["revisions"]],
            "decision_ids": [],
            "schedule_run_id": None,
            "sensitive_event_ids": [
                e["event_id"] for e in src.events if e.get("sensitive_payload_key_ref")
            ],
        },
        "verification": verification_meta,
        "resources": res,
    }
    # the manifest must be canonicalizable (no non-JSON values)
    canonical_json(manifest)
    return BuiltDocument(markdown, manifest, body_sha, sha)


def document_id_for_task(task_id: str) -> str:
    return "doc-" + hashlib.sha256(f"task|{task_id}".encode()).hexdigest()[:16]


__all__ = [
    "HEADINGS",
    "BuiltDocument",
    "DocumentBuildError",
    "SourceFreeze",
    "TaskSources",
    "build_skeleton",
    "collect_task_sources",
    "document_id_for_task",
    "freeze_for_task",
]
