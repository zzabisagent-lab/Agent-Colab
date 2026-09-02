"""Tests for the baseline document parsers and linters (P0-02 / V-P0-10, 14, 15, 20)."""

from __future__ import annotations

import pytest

from tools import criteria_lint, phase_dag_lint, plan_baseline_lint, trace_matrix
from tools.baseline import expand_ids, load_baseline, parse_tables, phase_of, size_weight


def test_expand_ids_handles_ranges_and_lists() -> None:
    assert expand_ids("P1-01, V-P2-20~22") == ["P1-01", "V-P2-20", "V-P2-21", "V-P2-22"]
    assert expand_ids("V-P0-01~03") == ["V-P0-01", "V-P0-02", "V-P0-03"]
    assert expand_ids("—") == []


def test_parse_tables_unescapes_pipes() -> None:
    md = "# H\n\n| a | b |\n|---|---|\n| `<x \\| y>` | 1 |\n"
    tables = parse_tables(md)
    assert tables[0].rows == [["`<x | y>`", "1"]]
    assert tables[0].heading == "H"


def test_phase_and_size_helpers() -> None:
    assert phase_of("V-P5-31") == 5
    assert phase_of("P0-14") == 0
    assert size_weight("L") == 5.0
    with pytest.raises(ValueError):
        phase_of("REQ-X")


def test_baseline_counts_match_documents() -> None:
    b = load_baseline()
    assert len(b.packages) == 103
    assert len(b.tests) == 230
    assert len(b.requirements) == 111
    assert b.packages["P1-08"].prereq == ["P1-02", "P1-03"]
    assert b.packages["P1-08"].size == "L"


def test_trace_matrix_has_no_problems() -> None:
    assert trace_matrix.build()["problems"] == []


def test_criteria_lint_flags_vague_only_and_accepts_deterministic() -> None:
    vague = criteria_lint.classify("handled appropriately per policy")
    assert vague["vague_only"] is True and vague["deterministic"] is False
    ok = criteria_lint.classify("exactly 3 redeliveries then EXPIRED; duplicates ignored")
    assert ok["deterministic"] is True and ok["vague_only"] is False
    assert {"numeric", "state", "invariant"} <= set(ok["categories"])  # type: ignore[arg-type]


def test_criteria_lint_passes_on_baseline() -> None:
    assert criteria_lint.main() == 0


def test_cycle_detection() -> None:
    assert plan_baseline_lint.topo_cycle({"a": ["b"], "b": ["c"], "c": ["a"]}) is not None
    assert plan_baseline_lint.topo_cycle({"a": ["b"], "b": []}) is None


def test_plan_and_dag_lint_pass_on_baseline() -> None:
    assert plan_baseline_lint.main() == 0
    assert phase_dag_lint.main() == 0
