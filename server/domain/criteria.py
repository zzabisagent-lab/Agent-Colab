"""Task acceptance criteria (P1-11; development plan §7D.1, spec §9.1 AcceptanceCriteria).

A Task needs at least one criterion before it can be delegated; ``implementation_submit`` must
attach an evidence reference for every *required* criterion of the pinned revision. Criteria are
pinned per revision (revision 1 in the TASK_CREATED payload, later revisions through the
``ACCEPTANCE_CRITERIA_REVISED`` Event) and never edited in place.

Evidence reference representation (``SubmitImplementation.evidence_refs``): each entry is either
``"<criteria_id>:<ref>"`` (evidence for that criterion) or a bare ``"<ref>"`` (general evidence
that does not satisfy any criterion). ``parse_evidence_refs`` splits them.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "api"
    / "task"
    / "acceptance-criteria.v1.schema.json"
)
CHECK_TYPES: tuple[str, ...] = ("evidence", "test_command", "artifact_hash", "human_attest")
STATEMENT_MAX_CHARS = 2000
CRITERIA_ID_PREFIX = "crit-"


class CriteriaError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class AcceptanceCriterion:
    criteria_id: str
    statement: str
    check_type: str
    required: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "criteria_id": self.criteria_id,
            "statement": self.statement,
            "check_type": self.check_type,
            "required": self.required,
        }


# Default templates per channel type (spec §8.1 templates; development plan §7D.1). Clients and
# the Mattermost `task create` flow use them to prefill ``--criteria``; a Task created without
# criteria stays criteria-less and cannot be delegated until a revision pins at least one entry.
DEFAULT_TEMPLATES: dict[str, tuple[dict[str, Any], ...]] = {
    "work": (
        {
            "statement": "Deliverable attached as an Artifact with its SHA-256 recorded",
            "check_type": "artifact_hash",
            "required": True,
        },
        {
            "statement": "Progress and result reported in the Task thread",
            "check_type": "evidence",
            "required": True,
        },
    ),
    "brainstorm": (
        {
            "statement": "Summary approved by the facilitator and recorded as an Artifact",
            "check_type": "evidence",
            "required": True,
        },
        {
            "statement": "Every Decision cites the Events it was derived from",
            "check_type": "human_attest",
            "required": False,
        },
    ),
    "approval": (
        {
            "statement": "The approved action was executed within the Approval scope",
            "check_type": "evidence",
            "required": True,
        },
    ),
    "ops": (
        {
            "statement": "Runbook step outputs captured with exit codes",
            "check_type": "test_command",
            "required": True,
        },
        {
            "statement": "Post-change health checks pass",
            "check_type": "test_command",
            "required": True,
        },
    ),
    "custom": (
        {
            "statement": "Result evidence attached and reviewed",
            "check_type": "evidence",
            "required": True,
        },
    ),
}


def default_criteria_for(channel_type: str) -> list[dict[str, Any]]:
    """Template entries for a channel type (``custom`` when unknown); copies, never shared."""
    template = DEFAULT_TEMPLATES.get(channel_type, DEFAULT_TEMPLATES["custom"])
    return [dict(entry) for entry in template]


def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


_VALIDATOR = _validator()


def validate_criteria(raw: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Validate raw entries against the JSON Schema; returns normalized dicts (no ids)."""
    entries = [dict(e) for e in (raw or ())]
    if not entries:
        raise CriteriaError("ACCEPTANCE_CRITERIA_REQUIRED", "at least one criterion is required")
    stripped = [{**e, "statement": str(e.get("statement", "")).strip()} for e in entries]
    errors = sorted(_VALIDATOR.iter_errors(stripped), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        path = "/".join(str(p) for p in first.path) or "<root>"
        raise CriteriaError("ACCEPTANCE_CRITERIA_INVALID", f"{path}: {first.message}")
    return [
        {
            "statement": e["statement"],
            "check_type": e["check_type"],
            "required": bool(e.get("required", True)),
        }
        for e in stripped
    ]


def criteria_id(task_id: str, revision: int, index: int, statement: str) -> str:
    digest = hashlib.sha256(f"{task_id}|{revision}|{index}|{statement}".encode()).hexdigest()
    return f"{CRITERIA_ID_PREFIX}{digest[:16]}"


def build_revision(
    task_id: str, revision: int, raw: Sequence[Mapping[str, Any]] | None
) -> list[AcceptanceCriterion]:
    """Validate and assign deterministic ids for one revision (ids ignore any client-sent id)."""
    if revision < 1:
        raise CriteriaError("ACCEPTANCE_CRITERIA_INVALID", "revision must be >= 1")
    entries = validate_criteria(raw)
    return [
        AcceptanceCriterion(
            criteria_id=criteria_id(task_id, revision, i, e["statement"]),
            statement=e["statement"],
            check_type=e["check_type"],
            required=e["required"],
        )
        for i, e in enumerate(entries)
    ]


def parse_evidence_refs(refs: Iterable[str]) -> tuple[dict[str, list[str]], list[str]]:
    """Split ``"<criteria_id>:<ref>"`` entries by criterion; bare refs are general evidence."""
    by_criterion: dict[str, list[str]] = {}
    general: list[str] = []
    for ref in refs:
        head, sep, tail = ref.partition(":")
        if sep and head.startswith(CRITERIA_ID_PREFIX) and tail:
            by_criterion.setdefault(head, []).append(tail)
        else:
            general.append(ref)
    return by_criterion, general


def evidence_satisfies(
    criteria: Sequence[AcceptanceCriterion], evidence_by_id: Mapping[str, Sequence[str]]
) -> list[str]:
    """IDs of *required* criteria without at least one evidence reference (empty = satisfied)."""
    return [
        c.criteria_id
        for c in criteria
        if c.required and not [r for r in evidence_by_id.get(c.criteria_id, ()) if str(r).strip()]
    ]
