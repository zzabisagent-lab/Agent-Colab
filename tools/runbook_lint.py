"""Runbook completeness and alert linkage (V-P7-21, development plan §20 P7-08).

Every operational runbook under ``docs/operations/runbooks/`` must declare an ``RB-`` id and carry
Detection, Isolation, Recovery and Post-verification procedures. Every *critical* alert declared in
``policy/alert-rules.yaml`` must name a runbook that exists, so an operator paged at night always
lands on a procedure. The alert file is owned by the observability package; until it exists this
check reports the runbooks it validated and skips the linkage half with a stated reason.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

from tools.baseline import ROOT

RUNBOOK_DIR = ROOT / "docs" / "operations" / "runbooks"
ALERT_RULES = ROOT / "policy" / "alert-rules.yaml"
REQUIRED_SECTIONS = ("Detection", "Isolation", "Recovery", "Post-verification")
EXPECTED_IDS = {
    "RB-SECRET-LEAK",
    "RB-NAS-FULL",
    "RB-BRIDGE-LOOP",
    "RB-SCHEDULER-STORM",
    "RB-DB-RESTORE",
    "RB-CREDENTIAL-ROTATION",
    "RB-HARD-DELETE-RESTORE",
}
ID_LINE = re.compile(r"^- \*\*Id:\*\* `(RB-[A-Z0-9-]+)`", re.M)
SECTION = re.compile(r"^## (.+)$", re.M)


def runbooks() -> dict[str, Path]:
    """Every runbook keyed by both its ``RB-`` id and its file slug.

    Alert rules reference a runbook by slug (``db-restore``); the documents declare an id
    (``RB-DB-RESTORE``). Both forms resolve here so neither side has to restate the other's
    convention.
    """
    found: dict[str, Path] = {}
    for path in sorted(RUNBOOK_DIR.glob("*.md")):
        match = ID_LINE.search(path.read_text(encoding="utf-8"))
        if match:
            found[match.group(1)] = path
            found[path.stem] = path
    return found


def _alert_document() -> dict[str, Any]:
    raw = yaml.safe_load(ALERT_RULES.read_text(encoding="utf-8")) or {}
    return raw if isinstance(raw, dict) else {"rules": raw}


def _critical_alerts(document: dict[str, Any]) -> list[dict[str, Any]]:
    rules = document.get("rules") or document.get("alerts") or []
    return [a for a in rules if str(a.get("severity", "")).lower() == "critical"]


def main() -> int:
    problems: list[str] = []
    found = runbooks()
    for path in sorted(RUNBOOK_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(ROOT)
        if not ID_LINE.search(text):
            problems.append(f"{rel}: no `- **Id:** \\`RB-...\\`` line")
            continue
        sections = set(SECTION.findall(text))
        missing = [s for s in REQUIRED_SECTIONS if s not in sections]
        if missing:
            problems.append(f"{rel}: missing section(s) {', '.join(missing)}")
    for required in sorted(EXPECTED_IDS - {k for k in found if k.startswith("RB-")}):
        problems.append(f"missing runbook {required} (development plan §20 P7-08 names seven)")

    linked = 0
    if not ALERT_RULES.exists():
        print(
            "runbook_lint: alert linkage skipped, policy/alert-rules.yaml does not exist yet "
            "(owned by the observability package)"
        )
    else:
        document = _alert_document()
        for declared in document.get("runbooks") or []:
            if str(declared) not in found:
                problems.append(f"alert rules declare runbook {declared}, which does not exist")
        for alert in _critical_alerts(document):
            key = str(alert.get("id") or alert.get("key") or alert.get("name") or "?")
            runbook = str(alert.get("runbook") or "")
            if not runbook:
                problems.append(f"critical alert {key}: no runbook id")
            elif runbook not in found:
                problems.append(f"critical alert {key}: runbook {runbook} does not exist")
            else:
                linked += 1
        print(f"runbook_lint: {linked} critical alerts linked to a runbook")

    for p in problems:
        print(p)
    ids = {k for k in found if k.startswith("RB-")}
    print(f"runbook_lint: {len(ids)} runbooks, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
