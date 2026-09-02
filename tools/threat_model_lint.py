"""Threat model checklist linter (V-P0-08).

Parses the boundary checklist table in ``docs/security/threat-model.md`` and fails unless the
mandatory boundaries (Mattermost, Telegram, Setup, Secret, Admin) are present with
``Included = yes``, non-empty Controls, and Test IDs that exist in ``docs/traceability.json``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from tools.baseline import ROOT, parse_tables

THREAT_MODEL = ROOT / "docs" / "security" / "threat-model.md"
TRACEABILITY = ROOT / "docs" / "traceability.json"
MANDATORY = ("Mattermost", "Telegram", "Setup", "Secret", "Admin")
_TEST_ID = re.compile(r"V-P\d-\d{2}")


def lint(markdown: str, known_tests: set[str]) -> list[str]:
    """Return a list of problems; empty means the checklist passes."""
    tables = [t for t in parse_tables(markdown) if t.header[:2] == ["Boundary", "Included"]]
    if not tables:
        return ["no checklist table with header | Boundary | Included | Controls | Tests |"]
    table = tables[-1]
    problems: list[str] = []
    rows: dict[str, list[str]] = {}
    for row in table.rows:
        if len(row) < 4:
            problems.append(f"row {row[:1]} has fewer than 4 cells")
            continue
        rows[row[0].strip()] = row
    for name in MANDATORY:
        if name not in rows:
            problems.append(f"mandatory boundary missing: {name}")
    for name, row in rows.items():
        included, controls, tests = row[1].strip().lower(), row[2].strip(), row[3].strip()
        if included != "yes":
            problems.append(f"{name}: Included must be 'yes', got {row[1]!r}")
        if not controls:
            problems.append(f"{name}: Controls empty")
        ids = _TEST_ID.findall(tests)
        if not ids:
            problems.append(f"{name}: no Test ID cited")
        for tid in ids:
            if tid not in known_tests:
                problems.append(f"{name}: unknown Test ID {tid}")
    return problems


def main(path: Path = THREAT_MODEL) -> int:
    known = set(json.loads(TRACEABILITY.read_text(encoding="utf-8"))["tests"])
    problems = lint(path.read_text(encoding="utf-8"), known)
    for p in problems:
        print(f"THREAT: {p}")
    print(f"threat_model_lint: {len(MANDATORY)} mandatory boundaries, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
