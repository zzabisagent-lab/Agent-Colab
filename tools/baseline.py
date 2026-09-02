"""Parsers for the protected v8 baseline documents (docs/baseline).

The three documents are the source of truth; these helpers only read them. Every linter in
``tools/`` (traceability, deterministic criteria, phase DAG, plan baseline) builds on this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_DIR = ROOT / "docs" / "baseline"
SPEC = BASELINE_DIR / "agent-colab-project-spec_en-v8.md"
DEV_PLAN = BASELINE_DIR / "agent-colab-development-plan_en-v8.md"
VALIDATION_PLAN = BASELINE_DIR / "agent-colab-validation-plan_en-v8.md"

_ID_RANGE = re.compile(r"\b(V-P\d-|P\d-)(\d{2})(?:~(\d{2}))?\b")
_SIZE_WEIGHT = {"S": 1.0, "M": 2.5, "L": 5.0}


@dataclass
class Table:
    """A markdown pipe table with its header cells, rows, and the nearest heading."""

    heading: str
    header: list[str]
    rows: list[list[str]] = field(default_factory=list)

    def column(self, name: str) -> int:
        for i, h in enumerate(self.header):
            if h.strip().lower() == name.lower():
                return i
        raise KeyError(f"column {name!r} not in {self.header}")


def _split_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    cells = re.split(r"(?<!\\)\|", body)
    return [c.replace("\\|", "|").strip() for c in cells]


def parse_tables(markdown: str) -> list[Table]:
    tables: list[Table] = []
    heading = ""
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
        if line.lstrip().startswith("|") and i + 1 < len(lines):
            sep = lines[i + 1]
            if re.match(r"^\s*\|?\s*:?-{3,}", sep):
                table = Table(heading=heading, header=_split_row(line))
                i += 2
                while i < len(lines) and lines[i].lstrip().startswith("|"):
                    table.rows.append(_split_row(lines[i]))
                    i += 1
                tables.append(table)
                continue
        i += 1
    return tables


def expand_ids(cell: str) -> list[str]:
    """Expand `P1-01, V-P2-20~22` into the individual IDs in document order."""
    out: list[str] = []
    for m in _ID_RANGE.finditer(cell):
        prefix, start, end = m.group(1), int(m.group(2)), m.group(3)
        last = int(end) if end else start
        for n in range(start, last + 1):
            out.append(f"{prefix}{n:02d}")
    return out


def phase_of(identifier: str) -> int:
    m = re.match(r"(?:V-)?P(\d)-", identifier)
    if not m:
        raise ValueError(identifier)
    return int(m.group(1))


def size_weight(size: str) -> float:
    return _SIZE_WEIGHT[size.strip()]


@dataclass
class Package:
    package_id: str
    work: str
    completion: str
    prereq: list[str]
    size: str
    tests: list[str]
    heading: str


@dataclass
class Test:
    test_id: str
    subject: str
    method: str
    criterion: str
    heading: str


@dataclass
class Requirement:
    req_id: str
    text: str
    spec_refs: str
    packages: list[str]
    tests: list[str]


@dataclass
class Baseline:
    packages: dict[str, Package]
    tests: dict[str, Test]
    requirements: dict[str, Requirement]
    dependencies: list[dict[str, str]]
    risk_rows: list[dict[str, str]]
    spec_risks: list[str]

    def package_ids(self) -> list[str]:
        return sorted(self.packages, key=lambda p: (phase_of(p), p))

    def test_ids(self) -> list[str]:
        return sorted(self.tests, key=lambda t: (phase_of(t), t))


def load_baseline() -> Baseline:
    spec_tables = parse_tables(SPEC.read_text(encoding="utf-8"))
    dev_tables = parse_tables(DEV_PLAN.read_text(encoding="utf-8"))
    val_tables = parse_tables(VALIDATION_PLAN.read_text(encoding="utf-8"))

    packages: dict[str, Package] = {}
    for t in dev_tables:
        if t.header[:1] == ["ID"] and "Prereq" in t.header:
            for r in t.rows:
                pid = r[0].strip("` ")
                packages[pid] = Package(
                    package_id=pid,
                    work=r[1],
                    completion=r[2],
                    prereq=expand_ids(r[3]),
                    size=r[4].strip(),
                    tests=expand_ids(r[5]),
                    heading=t.heading,
                )

    tests: dict[str, Test] = {}
    for t in val_tables:
        if t.header[:1] == ["Test ID"]:
            for r in t.rows:
                tid = r[0].strip("` ")
                tests[tid] = Test(
                    test_id=tid, subject=r[1], method=r[2], criterion=r[3], heading=t.heading
                )

    requirements: dict[str, Requirement] = {}
    for t in spec_tables:
        if t.header[:1] == ["REQ ID"]:
            for r in t.rows:
                requirements[r[0]] = Requirement(
                    req_id=r[0],
                    text=r[1],
                    spec_refs=r[2],
                    packages=expand_ids(r[3]),
                    tests=expand_ids(r[4]),
                )

    dependencies: list[dict[str, str]] = []
    risk_rows: list[dict[str, str]] = []
    for t in dev_tables:
        if t.header[:1] == ["Dependency"]:
            dependencies.extend(dict(zip(t.header, r, strict=False)) for r in t.rows)
        if t.header[:1] == ["Risk"] and "Mitigating packages" in t.header:
            risk_rows.extend(dict(zip(t.header, r, strict=False)) for r in t.rows)

    spec_risks: list[str] = []
    for t in spec_tables:
        if t.header[:2] == ["Risk", "Mitigation"]:
            spec_risks.extend(r[0] for r in t.rows)

    return Baseline(packages, tests, requirements, dependencies, risk_rows, spec_risks)
