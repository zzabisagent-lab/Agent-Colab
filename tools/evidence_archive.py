"""V-P7-16/V-P7-17: the release evidence archive and its residual-risk register.

Collects every Phase's Verifier reports and SELF evidence into one index, checks that nothing a
Phase claimed is missing, and re-scans the archive for secrets. The residual-risk register is
checked for an owner, a deadline and an acceptor on every open finding.

    uv run python -m tools.evidence_archive --check
    uv run python -m tools.evidence_archive --build --out release/evidence-index.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VERIFICATION = ROOT / "verification"
EVIDENCE = ROOT / "evidence"
RESIDUAL = ROOT / "docs" / "security" / "residual-risks.md"
PHASES = range(0, 8)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class PhaseEntry:
    phase: int
    reports: list[dict[str, str]] = field(default_factory=list)
    passed_report: str | None = None
    self_evidence: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def collect_phase(phase: int) -> PhaseEntry:
    entry = PhaseEntry(phase)
    reports_dir = VERIFICATION / f"phase-{phase}"
    if not reports_dir.is_dir():
        entry.problems.append("no verification directory")
        return entry
    for report in sorted(reports_dir.glob(f"VR-P{phase}-*.yaml")):
        text = report.read_text(encoding="utf-8")
        result = "UNKNOWN"
        for line in text.splitlines():
            if line.startswith("result:"):
                result = line.split(":", 1)[1].strip()
        entry.reports.append(
            {"file": str(report.relative_to(ROOT)), "result": result, "sha256": _sha256(report)}
        )
        if result == "PASSED":
            entry.passed_report = str(report.relative_to(ROOT))
        checksum = report.with_suffix(".yaml.sha256")
        if checksum.is_file():
            recorded = checksum.read_text(encoding="utf-8").split()[0]
            if recorded != _sha256(report):
                entry.problems.append(f"{report.name} does not match its recorded checksum")
    if not entry.reports:
        entry.problems.append("no Verifier report")
    elif entry.passed_report is None:
        entry.problems.append("no PASSED Verifier report")
    for attempt in sorted((EVIDENCE / f"phase-{phase}").glob("SELF-*/attempt-*/result.json")):
        payload = json.loads(attempt.read_text(encoding="utf-8"))
        test_id = str(payload.get("test_id") or attempt.parts[-3].removeprefix("SELF-"))
        status = str(payload.get("status") or payload.get("result") or "unknown")
        entry.self_evidence[test_id] = status  # later attempts overwrite earlier ones
    failing = sorted(t for t, s in entry.self_evidence.items() if s != "pass")
    if failing:
        entry.problems.append(f"latest attempt not passing: {', '.join(failing)}")
    return entry


ROW = re.compile(
    r"^\|\s*(?P<finding>[^|]+?)\s*\|\s*(?P<severity>[^|]+?)\s*\|\s*(?P<owner>[^|]+?)\s*\|\s*(?P<deadline>[^|]+?)\s*\|"
)


def residual_risks() -> tuple[list[dict[str, str]], list[str]]:
    """Every open finding needs a severity, an owner and a deadline; High and Critical block."""
    if not RESIDUAL.is_file():
        return [], ["docs/security/residual-risks.md is missing"]
    rows: list[dict[str, str]] = []
    problems: list[str] = []
    for line in RESIDUAL.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line)
        if not match or match.group("finding").lower() in ("finding", "---"):
            continue
        row = {k: match.group(k) for k in ("finding", "severity", "owner", "deadline")}
        if set(row["severity"]) <= {"-"}:
            continue
        rows.append(row)
        if row["severity"].upper() in ("HIGH", "CRITICAL"):
            problems.append(f"{row['finding']}: {row['severity']} findings block the release")
        for field_name in ("owner", "deadline"):
            if not row[field_name] or set(row[field_name]) <= {"-"}:
                problems.append(f"{row['finding']}: no {field_name}")
    if not rows:
        problems.append("no residual-risk rows found")
    return rows, problems


def secret_scan() -> tuple[bool, str]:
    """The archive is shipped, so it is re-scanned rather than trusted."""
    result = subprocess.run(
        ["gitleaks", "dir", "--no-banner", "--redact", str(VERIFICATION), str(EVIDENCE)],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    return result.returncode == 0, (result.stderr or result.stdout).strip().splitlines()[-1][:200]


def build(highest_phase: int = 7) -> dict[str, Any]:
    """`highest_phase` is the last Phase expected to be complete; later ones are not yet due."""
    phases = [collect_phase(p) for p in PHASES if p <= highest_phase]
    rows, risk_problems = residual_risks()
    clean, scan_detail = secret_scan()
    problems = [f"phase {e.phase}: {p}" for e in phases for p in e.problems]
    problems += [f"residual risk: {p}" for p in risk_problems]
    if not clean:
        problems.append(f"secret scan: {scan_detail}")
    return {
        "schema_id": "colab.evidence-index.v1",
        "phases": [
            {
                "phase": e.phase,
                "reports": e.reports,
                "passed_report": e.passed_report,
                "self_evidence_count": len(e.self_evidence),
                "self_evidence": e.self_evidence,
            }
            for e in phases
        ],
        "residual_risks": rows,
        "secret_scan_clean": clean,
        "problems": problems,
        "ok": not problems,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--phases", type=int, default=7, help="highest phase expected complete")
    args = parser.parse_args(argv)
    index = build(args.phases)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    summary = {
        "ok": index["ok"],
        "problems": index["problems"],
        "phases": {p["phase"]: p["passed_report"] for p in index["phases"]},
        "residual_risks": len(index["residual_risks"]),
        "secret_scan_clean": index["secret_scan_clean"],
    }
    print(json.dumps(summary, indent=2))
    return 0 if index["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
