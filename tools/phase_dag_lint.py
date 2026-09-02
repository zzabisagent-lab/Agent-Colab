"""Phase dependency DAG linter (V-P0-15).

For every work package and Test, every entity referenced in its text must be introduced in an
earlier or the same phase, except explicit contract stubs listed in ``docs/phase-entities.yaml``.
Phase 0 defines contracts and may reference any entity. Package prerequisites must also point to
the same or an earlier phase.
"""

from __future__ import annotations

import re
import sys

import yaml

from tools.baseline import ROOT, load_baseline, phase_of

CONFIG = ROOT / "docs" / "phase-entities.yaml"


def _pattern(entity: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(entity)}(?![\w-])")


def main() -> int:
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    entities: dict[str, int] = {str(k): int(v) for k, v in cfg["entities"].items()}
    stubs: dict[str, set[str]] = {}
    for s in cfg.get("stubs", []):
        stubs.setdefault(s["id"], set()).update(s["entities"])
    patterns = {e: _pattern(e) for e in entities}
    b = load_baseline()
    problems: list[str] = []
    checked = 0

    def check(item_id: str, text: str) -> None:
        nonlocal checked
        checked += 1
        phase = phase_of(item_id)
        if phase == 0:
            return
        for entity, intro in entities.items():
            if intro > phase and patterns[entity].search(text):
                if entity in stubs.get(item_id, set()):
                    continue
                problems.append(
                    f"{item_id} (Phase {phase}) references {entity!r} introduced in Phase {intro}"
                )

    for p in b.packages.values():
        check(p.package_id, f"{p.work} {p.completion}")
        for q in p.prereq:
            if q in b.packages and phase_of(q) > phase_of(p.package_id):
                problems.append(f"{p.package_id}: prerequisite {q} is a forward dependency")
        for tid in p.tests:
            if phase_of(tid) != phase_of(p.package_id):
                problems.append(f"{p.package_id}: Test {tid} belongs to another phase")
    for test in b.tests.values():
        check(test.test_id, f"{test.subject} {test.method} {test.criterion}")
    for pr in problems:
        print(f"DAG: {pr}")
    print(
        f"phase_dag_lint: {checked} items checked, {len(stubs)} explicit stubs, "
        f"{len(problems)} problems"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
