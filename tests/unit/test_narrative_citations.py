"""P6-10 citation linter (V-P6-28).

A narrative paragraph without a citation, one citing an id outside the freeze, and one restating
a figure the skeleton computed differently are all rejected; a skeleton-only document stays valid
and an accepted narrative never overwrites a structured fact.
"""

from __future__ import annotations

from typing import Any

from server.documents.citations import (
    REASON_CONTRADICTS_SKELETON,
    REASON_MISSING_CITATION,
    REASON_UNKNOWN_REFERENCE,
    lint,
    skeleton_facts,
)

KNOWN = [("evt", "evt-1"), ("evt", "evt-2"), ("art", "art-9"), ("vr", "vr-3")]
MANIFEST: dict[str, Any] = {
    "provenance": {"event_ids": ["evt-1", "evt-2"], "artifact_ids": ["art-9"]},
    "resources": {"cost_units": 4200, "input_tokens": 10, "output_tokens": 5, "tool_calls": 2},
    "verification": {"findings": 3},
}


def test_skeleton_facts_are_taken_from_the_manifest() -> None:
    facts = skeleton_facts(MANIFEST)
    assert facts["event_count"] == 2
    assert facts["artifact_count"] == 1
    assert facts["finding_count"] == 3
    assert facts["cost_units"] == 4200


def test_a_paragraph_without_a_citation_is_rejected() -> None:
    result = lint(
        "The team decided to ship early.", known_refs=KNOWN, facts=skeleton_facts(MANIFEST)
    )
    assert not result.ok
    assert result.reason_code == REASON_MISSING_CITATION


def test_an_unknown_reference_is_rejected() -> None:
    body = "The rollback was chosen [[evt:evt-does-not-exist]]."
    result = lint(body, known_refs=KNOWN, facts=skeleton_facts(MANIFEST))
    assert not result.ok
    assert result.reason_code == REASON_UNKNOWN_REFERENCE


def test_a_figure_contradicting_the_skeleton_is_rejected() -> None:
    body = "The run consumed 9999 cost_units [[evt:evt-1]]."
    result = lint(body, known_refs=KNOWN, facts=skeleton_facts(MANIFEST))
    assert not result.ok
    assert result.reason_code == REASON_CONTRADICTS_SKELETON
    assert "skeleton says 4200" in result.errors[0].detail


def test_restating_a_figure_correctly_is_accepted() -> None:
    body = "The run consumed 4200 cost_units [[evt:evt-1]] across 2 events [[evt:evt-2]]."
    result = lint(body, known_refs=KNOWN, facts=skeleton_facts(MANIFEST))
    assert result.ok, [e.detail for e in result.errors]


def test_every_paragraph_needs_its_own_citation() -> None:
    body = "First point [[evt:evt-1]].\n\nSecond point with no source at all."
    result = lint(body, known_refs=KNOWN, facts=skeleton_facts(MANIFEST))
    assert not result.ok
    assert [e.paragraph for e in result.errors] == [1]


def test_a_well_cited_narrative_reports_its_citations() -> None:
    body = (
        "The approach was chosen over the alternative [[evt:evt-1]] [[vr:vr-3]].\n\n"
        "Its output is attached [[art:art-9]]."
    )
    result = lint(body, known_refs=KNOWN, facts=skeleton_facts(MANIFEST))
    assert result.ok
    assert result.citations == [("evt", "evt-1"), ("vr", "vr-3"), ("art", "art-9")]


def test_an_empty_narrative_is_valid_so_skeleton_only_documents_pass() -> None:
    assert lint("", known_refs=KNOWN, facts=skeleton_facts(MANIFEST)).ok
    assert lint("   \n\n  ", known_refs=KNOWN).ok
