"""P1-11 unit tests: schema validation, deterministic ids, templates, evidence matching."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from server.domain.criteria import (
    CHECK_TYPES,
    DEFAULT_TEMPLATES,
    AcceptanceCriterion,
    CriteriaError,
    build_revision,
    criteria_id,
    default_criteria_for,
    evidence_satisfies,
    parse_evidence_refs,
    validate_criteria,
)

CASES = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "fixtures" / "tasks" / "criteria-cases.yaml").read_text(
        encoding="utf-8"
    )
)


def _materialize(case: dict[str, Any]) -> list[dict[str, Any]]:
    criteria = [dict(c) for c in case["criteria"]]
    if "long_statement" in case:
        criteria[0]["statement"] = "x" * int(case["long_statement"])
    if "repeat" in case:
        criteria = [{"statement": f"c{i}", "check_type": "evidence"} for i in range(case["repeat"])]
    return criteria


@pytest.mark.parametrize("case", CASES["valid"], ids=[c["name"] for c in CASES["valid"]])
def test_valid_criteria_normalize(case: dict[str, Any]) -> None:
    entries = validate_criteria(_materialize(case))
    assert entries and all(e["statement"] == e["statement"].strip() for e in entries)
    assert all(e["check_type"] in CHECK_TYPES for e in entries)
    assert all(isinstance(e["required"], bool) for e in entries)


@pytest.mark.parametrize("case", CASES["invalid"], ids=[c["name"] for c in CASES["invalid"]])
def test_invalid_criteria_have_stable_codes(case: dict[str, Any]) -> None:
    with pytest.raises(CriteriaError) as exc:
        validate_criteria(_materialize(case))
    assert exc.value.code == case["code"]


def test_ids_are_deterministic_and_revision_scoped() -> None:
    raw: list[dict[str, Any]] = [
        {"statement": "tests pass", "check_type": "test_command"},
        {"statement": "docs updated", "check_type": "evidence", "required": False},
    ]
    a = build_revision("task-1", 1, raw)
    b = build_revision("task-1", 1, raw)
    assert [c.criteria_id for c in a] == [c.criteria_id for c in b]
    assert a[0].criteria_id == criteria_id("task-1", 1, 0, "tests pass")
    assert a[0].criteria_id != build_revision("task-1", 2, raw)[0].criteria_id
    assert a[0].criteria_id != build_revision("task-2", 1, raw)[0].criteria_id
    assert a[0].criteria_id.startswith("crit-") and len(a[0].criteria_id) == 21
    assert a[1].required is False and a[0].required is True
    # client-sent ids are ignored (server-assigned)
    forged = build_revision("task-1", 1, [{**raw[0], "criteria_id": "crit-" + "0" * 16}])
    assert forged[0].criteria_id == a[0].criteria_id
    with pytest.raises(CriteriaError):
        build_revision("task-1", 0, raw)


@pytest.mark.parametrize("channel_type", ["work", "brainstorm", "approval", "ops", "custom"])
def test_default_templates_are_valid_per_channel_type(channel_type: str) -> None:
    template = default_criteria_for(channel_type)
    assert template == list(DEFAULT_TEMPLATES[channel_type])
    assert validate_criteria(template)
    assert any(e.get("required", True) for e in template)
    template[0]["statement"] = "mutated"
    assert DEFAULT_TEMPLATES[channel_type][0]["statement"] != "mutated"  # copies


def test_unknown_channel_type_falls_back_to_custom() -> None:
    assert default_criteria_for("weird") == default_criteria_for("custom")


def test_parse_and_satisfy_evidence_refs() -> None:
    crit = [
        AcceptanceCriterion("crit-" + "a" * 16, "a", "evidence", True),
        AcceptanceCriterion("crit-" + "b" * 16, "b", "evidence", False),
        AcceptanceCriterion("crit-" + "c" * 16, "c", "artifact_hash", True),
    ]
    by_id, general = parse_evidence_refs(
        ["crit-" + "a" * 16 + ":art-1", "art-general", "crit-" + "b" * 16 + ":thread-post-9", "x:y"]
    )
    assert general == ["art-general", "x:y"]
    assert by_id == {"crit-" + "a" * 16: ["art-1"], "crit-" + "b" * 16: ["thread-post-9"]}
    assert evidence_satisfies(crit, by_id) == ["crit-" + "c" * 16]
    assert evidence_satisfies(crit, {**by_id, "crit-" + "c" * 16: ["  "]}) == ["crit-" + "c" * 16]
    assert evidence_satisfies(crit, {**by_id, "crit-" + "c" * 16: ["sha256:abc"]}) == []
    assert evidence_satisfies([], {}) == []
