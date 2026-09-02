"""Deterministic PASS-criterion linter (V-P0-14).

Every mandatory Test criterion must contain at least one applicable numeric, state, error-code,
hash-equality, or invariant criterion, and zero criteria may be judged only by words such as
"appropriately", "as far as possible", or "per policy". The classification is written to
``docs/criteria-lint.json`` so a Verifier can audit every decision.
"""

from __future__ import annotations

import json
import re
import sys

from tools.baseline import ROOT, load_baseline

OUT = ROOT / "docs" / "criteria-lint.json"

NUMERIC = re.compile(r"(\d|\bzero\b|\bone\b|\btwo\b|\bthree\b|\bten\b|≤|≥|%|\bp95\b|\bwithin\b)")
STATE = re.compile(
    r"\b(PASSED|FAILED|BLOCKED|SKIPPED|CANCELLED|COMPLETED|WAITING|RUNNING|VERIFIED|APPROVED|"
    r"PAUSED|EXPIRED|REVOKED|LOCKED|CONFIGURED|DEGRADED|DRAFT_PRE_VERIFICATION|ATTEMPT_FINALIZED|"
    r"FINALIZED|pending_admin|active|offline|online|status|state)\b"
)
ERROR_CODE = re.compile(
    r"`[A-Z][A-Z0-9_]{3,}`|\b[A-Z]{3,}(?:_[A-Z0-9]+)+\b|\b40[1-4]\b|\b429\b|\b503\b"
)
HASH = re.compile(r"\b(hash|checksum|byte-for-byte|identical|digest|sha-?256|snapshot)\b", re.I)
INVARIANT = re.compile(
    r"\b(zero|exactly|exact|only|never|all|every|unchanged|immutable|rejected|reject|no\b|none|not|"
    r"cannot|preserved|consistent|match(?:es)?|equal|identical|same|succeeds?|pass(?:es)?|blocked|"
    r"denied|unique|monotonic|100%|wins|is|are|included|includes|accepted|stable error|audited|"
    r"ignored|excluded|untranslated|displayed|linked|kept|reported|marked|protected|correct|clear|"
    r"requested|created|without|deny|denies|redacted|bidirectional|provenance)\b",
    re.I,
)
VAGUE = re.compile(
    r"\b(appropriately|as far as possible|per policy|properly|reasonabl\w*|adequate\w*)\b", re.I
)


def classify(criterion: str) -> dict[str, object]:
    cats = {
        "numeric": bool(NUMERIC.search(criterion)),
        "state": bool(STATE.search(criterion)),
        "error_code": bool(ERROR_CODE.search(criterion)),
        "hash_equality": bool(HASH.search(criterion)),
        "invariant": bool(INVARIANT.search(criterion)),
    }
    vague = VAGUE.findall(criterion)
    deterministic = any(cats.values())
    return {
        "categories": [k for k, v in cats.items() if v],
        "vague_words": vague,
        "deterministic": deterministic,
        "vague_only": bool(vague) and not deterministic,
    }


def main() -> int:
    b = load_baseline()
    report: dict[str, dict[str, object]] = {}
    problems: list[str] = []
    for t in b.tests.values():
        c = classify(t.criterion)
        report[t.test_id] = {"criterion": t.criterion, **c}
        if not c["deterministic"]:
            problems.append(f"{t.test_id}: no numeric/state/error-code/hash/invariant criterion")
        if c["vague_only"]:
            problems.append(f"{t.test_id}: judged only by vague words {c['vague_words']}")
    OUT.write_text(
        json.dumps({"tests": report, "problems": problems}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    for p in problems:
        print(f"CRITERIA: {p}")
    vague_present = sum(1 for r in report.values() if r["vague_words"])
    print(
        f"criteria_lint: {len(report)} tests, {len(problems)} problems, "
        f"{vague_present} criteria contain a vague word but also a deterministic criterion"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
