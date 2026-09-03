"""Citation linter for the narrative layer (development plan §10.4 layer 2, V-P6-28).

Layer 2 may only *explain* what layer 1 already established. Three rules enforce that:

1. every paragraph carries at least one citation ``[[evt:…]]``/``[[art:…]]``/``[[dec:…]]``/
   ``[[vr:…]]`` (``run``/``msg`` are accepted too — the pipeline records the same ref types);
2. every cited id exists in the frozen source set;
3. no figure in the narrative contradicts a structured fact of the skeleton.

Rule 3 compares numbers the narrative attributes to a known fact name against the value the
skeleton computed. A narrative that merely omits a figure is fine; one that restates it wrongly
is rejected, so the narrative can never overwrite a skeleton fact.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from server.documents.provenance import CITATION

REASON_MISSING_CITATION = "NARRATIVE_CITATION_MISSING"
REASON_UNKNOWN_REFERENCE = "NARRATIVE_CITATION_UNKNOWN"
REASON_CONTRADICTS_SKELETON = "NARRATIVE_CONTRADICTS_SKELETON"

# Fact names the narrative may restate, mapped to how they read in prose.
FACT_ALIASES: dict[str, tuple[str, ...]] = {
    "event_count": ("events", "event"),
    "artifact_count": ("artifacts", "artifact"),
    "finding_count": ("findings", "finding"),
    "cost_units": ("cost_units", "cost units"),
    "input_tokens": ("input tokens",),
    "output_tokens": ("output tokens",),
    "tool_calls": ("tool calls",),
}
_NUMBER = r"(\d[\d,]*)"


@dataclass(frozen=True)
class LintError:
    reason_code: str
    detail: str
    paragraph: int


@dataclass(frozen=True)
class LintResult:
    ok: bool
    errors: list[LintError] = field(default_factory=list)
    citations: list[tuple[str, str]] = field(default_factory=list)

    @property
    def reason_code(self) -> str | None:
        return self.errors[0].reason_code if self.errors else None


def paragraphs_of(body: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]


def skeleton_facts(manifest: Mapping[str, Any]) -> dict[str, int]:
    """The structured figures a narrative is allowed to restate, taken from the built manifest."""
    prov = manifest.get("provenance", {}) or {}
    res = manifest.get("resources", {}) or {}
    ver = manifest.get("verification") or {}
    facts: dict[str, int] = {
        "event_count": len(prov.get("event_ids", []) or []),
        "artifact_count": len(prov.get("artifact_ids", []) or []),
    }
    if isinstance(ver, Mapping) and isinstance(ver.get("findings"), int):
        facts["finding_count"] = int(ver["findings"])
    for key in ("cost_units", "input_tokens", "output_tokens", "tool_calls"):
        value = res.get(key)
        if isinstance(value, int):
            facts[key] = value
    return facts


def _contradictions(paragraph: str, facts: Mapping[str, int]) -> list[str]:
    """Numbers the paragraph attributes to a known fact that disagree with the skeleton."""
    found: list[str] = []
    lowered = paragraph.lower()
    for fact, aliases in FACT_ALIASES.items():
        if fact not in facts:
            continue
        for alias in aliases:
            for pattern in (
                rf"{_NUMBER}\s+{re.escape(alias)}\b",
                rf"{re.escape(alias)}\s*(?:is|was|=|:)?\s*{_NUMBER}\b",
            ):
                for raw in re.findall(pattern, lowered):
                    value = int(str(raw).replace(",", ""))
                    if value != facts[fact]:
                        found.append(f"{fact}: narrative says {value}, skeleton says {facts[fact]}")
    return found


def lint(
    body: str,
    *,
    known_refs: Iterable[tuple[str, str]],
    facts: Mapping[str, int] | None = None,
) -> LintResult:
    """Check a narrative body against the frozen sources and the skeleton's figures."""
    known = {(t, i) for t, i in known_refs}
    facts = facts or {}
    errors: list[LintError] = []
    citations: list[tuple[str, str]] = []
    paras = paragraphs_of(body)
    if not paras:
        return LintResult(True, [], [])
    for index, para in enumerate(paras):
        cites = [(t, i) for t, i in CITATION.findall(para)]
        if not cites:
            errors.append(
                LintError(REASON_MISSING_CITATION, f"paragraph {index + 1} cites no source", index)
            )
        for ref in cites:
            if ref not in known:
                errors.append(
                    LintError(
                        REASON_UNKNOWN_REFERENCE, f"{ref[0]}:{ref[1]} is not in the freeze", index
                    )
                )
            elif ref not in citations:
                citations.append(ref)
        for detail in _contradictions(para, facts):
            errors.append(LintError(REASON_CONTRADICTS_SKELETON, detail, index))
    return LintResult(not errors, errors, citations)


__all__ = [
    "FACT_ALIASES",
    "REASON_CONTRADICTS_SKELETON",
    "REASON_MISSING_CITATION",
    "REASON_UNKNOWN_REFERENCE",
    "LintError",
    "LintResult",
    "lint",
    "paragraphs_of",
    "skeleton_facts",
]
