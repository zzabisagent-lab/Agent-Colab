"""Development plan operating-baseline linter (V-P0-20, P0-14).

Checks: every P-ID has a size (S/M/L) and ≥ 1 Test ID; the prerequisite DAG is acyclic and only
references defined packages; every V-ID is back-linked to ≥ 1 P-ID; every §25A risk row names
≥ 1 package and ≥ 1 Test and covers every spec §18 risk; every §25 dependency row has an owner
and a deadline.
"""

from __future__ import annotations

import sys

from tools.baseline import load_baseline, phase_of, size_weight


def topo_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """Return a cycle path if one exists, else None."""
    color: dict[str, int] = {}
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        color[n] = 1
        stack.append(n)
        for m in graph.get(n, []):
            if color.get(m, 0) == 1:
                return [*stack[stack.index(m) :], m]
            if color.get(m, 0) == 0:
                found = visit(m)
                if found:
                    return found
        stack.pop()
        color[n] = 2
        return None

    for node in graph:
        if color.get(node, 0) == 0:
            found = visit(node)
            if found:
                return found
    return None


def main() -> int:
    b = load_baseline()
    problems: list[str] = []
    graph: dict[str, list[str]] = {}
    tests_covered: dict[str, list[str]] = {}
    for p in b.packages.values():
        try:
            size_weight(p.size)
        except KeyError:
            problems.append(f"{p.package_id}: size {p.size!r} not in S/M/L")
        if not p.tests:
            problems.append(f"{p.package_id}: no Test ID")
        for t in p.tests:
            tests_covered.setdefault(t, []).append(p.package_id)
            if t not in b.tests:
                problems.append(f"{p.package_id}: undefined Test {t}")
        for q in p.prereq:
            if q not in b.packages:
                problems.append(f"{p.package_id}: undefined prerequisite {q}")
            elif phase_of(q) > phase_of(p.package_id):
                problems.append(f"{p.package_id}: prerequisite {q} is in a later phase")
        graph[p.package_id] = list(p.prereq)
    cycle = topo_cycle(graph)
    if cycle:
        problems.append(f"prerequisite DAG has a cycle: {' -> '.join(cycle)}")
    for t in b.tests:
        if t not in tests_covered:
            problems.append(f"{t}: Test not mapped to any package")
    for row in b.risk_rows:
        pk = [x for x in row.get("Mitigating packages", "").split(",") if x.strip()]
        if not pk:
            problems.append(f"§25A '{row['Risk']}': no mitigating package")
        if not row.get("Judging Tests", "").strip():
            problems.append(f"§25A '{row['Risk']}': no judging Test")
    plan_risks = {r["Risk"] for r in b.risk_rows}
    for risk in b.spec_risks:
        if risk not in plan_risks:
            problems.append(f"spec §18 risk not in §25A: {risk!r}")
    for row in b.dependencies:
        if not row.get("Owner", "").strip():
            problems.append(f"§25 '{row['Dependency']}': no owner")
        if not row.get("Deadline", "").strip():
            problems.append(f"§25 '{row['Dependency']}': no deadline")
    for problem in problems:
        print(f"PLAN: {problem}")
    total = sum(size_weight(p.size) for p in b.packages.values() if p.size in "SML")
    print(
        f"plan_baseline_lint: {len(b.packages)} packages (weight {total:g}), {len(b.tests)} tests, "
        f"{len(b.risk_rows)} risk rows, {len(b.dependencies)} dependency rows, "
        f"{len(problems)} problems"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
