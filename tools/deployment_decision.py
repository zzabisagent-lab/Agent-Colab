"""V-P7-18: the deployment decision record, and proof that no deployment preceded it.

Reads ``docs/operations/deployment-decision.md``, checks the record against the state table it
documents, verifies the final report exists with the sections development plan §27A requires, and
checks the deployment ledger is empty unless an approved decision permits entries.

    uv run python -m tools.deployment_decision --check
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "operations" / "deployment-decision.md"
REPORT = ROOT / "REPORT.md"
LEDGER = ROOT / "release" / "deployments"

STATES = ("PENDING_USER_DECISION", "APPROVED", "DECLINED", "DEPLOYED")
DEPLOYABLE = ("APPROVED", "DEPLOYED")
FIELDS = (
    "state",
    "report",
    "report_delivered_at",
    "target",
    "decision",
    "decided_at",
    "decided_by",
    "deployment_actions",
)
#: development plan §27A: what the final report must contain.
REPORT_SECTIONS = (
    "Phase summary",
    "Acceptance status",
    "Residual risks",
    "Release artifacts",
    "Deployment plan",
)
BLOCK = re.compile(r"```yaml\n(.*?)```", re.S)


def read_record(path: Path = RECORD) -> dict[str, str]:
    """Parse the single fenced block of the decision record into flat string values."""
    match = BLOCK.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"{path} carries no decision block")
    record: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(":")
        record[key.strip()] = value.strip()
    return record


def ledger_entries(directory: Path = LEDGER) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if p.suffix == ".json")


def report_sections(path: Path = REPORT) -> list[str]:
    if not path.is_file():
        return []
    return [
        line.lstrip("# ").strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    ]


def check() -> tuple[dict[str, Any], list[str]]:
    problems: list[str] = []
    if not RECORD.is_file():
        return {}, [f"{RECORD.relative_to(ROOT)} is missing"]
    record = read_record()
    for field in FIELDS:
        if field not in record:
            problems.append(f"decision record has no {field!r} field")
    state = record.get("state", "")
    if state not in STATES:
        problems.append(f"state {state!r} is not one of {STATES}")

    if not REPORT.is_file():
        problems.append("REPORT.md is missing; the report is delivered before the decision")
    else:
        headings = " | ".join(report_sections())
        for section in REPORT_SECTIONS:
            if section.lower() not in headings.lower():
                problems.append(f"REPORT.md has no {section!r} section (development plan §27A)")

    entries = ledger_entries()
    if state not in DEPLOYABLE and entries:
        problems.append(
            f"{len(entries)} deployment ledger entries exist under state {state!r}: "
            "a deployment preceded the user's approval"
        )
    if state in DEPLOYABLE:
        for field in ("decision", "decided_at", "decided_by"):
            if record.get(field) in (None, "", "none", "null"):
                problems.append(f"state {state!r} requires {field!r}")
        if record.get("target") in (None, "", "none", "none recorded", "null"):
            problems.append(f"state {state!r} requires a named target")
    else:
        if record.get("decision") not in ("none", "declined"):
            recorded = record.get("decision")
            problems.append(f"state {state!r} must not record an approval ({recorded!r})")
    if state == "DEPLOYED" and not entries:
        problems.append("state 'DEPLOYED' records no ledger entry")

    summary = {
        "state": state,
        "report_present": REPORT.is_file(),
        "report_sections": report_sections(),
        "ledger_entries": entries,
        "deployment_performed": bool(entries),
        "ok": not problems,
        "problems": problems,
    }
    return summary, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="exit non-zero when the record has a problem"
    )
    args = parser.parse_args(argv)
    summary, problems = check()
    print(json.dumps(summary, indent=2))
    return 1 if (args.check and problems) else 0


if __name__ == "__main__":
    sys.exit(main())
