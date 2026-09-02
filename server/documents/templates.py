"""Canonical Markdown skeleton (spec §14.1) rendered by a deterministic template engine.

No LLM is involved (development plan §10.4 layer 1). Heading *keys* are stable identifiers;
localized heading text is a Phase 2 (P2-16) concern and must map onto these keys.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

TEMPLATE_VERSION = "task-skeleton-v1"

# (key, heading) in the mandatory order of spec §14.1
SECTIONS: tuple[tuple[str, str], ...] = (
    ("purpose", "Purpose and Scope"),
    ("participants", "Participants and Roles"),
    ("inputs", "Inputs and Resources Used"),
    ("process", "Process and Key Events"),
    ("discussion", "Discussion, Alternatives, Decisions and Rationale"),
    ("results", "Results and Artifacts"),
    ("verification", "Verification Method and Results"),
    ("shortcomings", "Shortcomings, Risks and Open Questions"),
    ("followup", "Follow-up Work"),
    ("provenance", "Provenance"),
)
SECTION_KEYS: tuple[str, ...] = tuple(k for k, _ in SECTIONS)
HEADINGS: dict[str, str] = dict(SECTIONS)
EMPTY_MARKER = "_none recorded_"
# i18n message key per section (development plan §7H: document headings are localized)
MESSAGE_KEYS: dict[str, str] = {
    "purpose": "document.section.purpose",
    "participants": "document.section.participants",
    "inputs": "document.section.inputs",
    "process": "document.section.process",
    "discussion": "document.section.discussion",
    "results": "document.section.results",
    "verification": "document.section.verification",
    "shortcomings": "document.section.risks",
    "followup": "document.section.followup",
    "provenance": "document.section.provenance",
}


def heading_for(key: str, language: str | None) -> str:
    """Localized heading text; English (the canonical text of spec §14.1) by default."""
    if language in (None, "en"):
        return HEADINGS[key]
    from server.i18n import translate

    return translate(MESSAGE_KEYS[key], language)


def render(title: str, sections: Mapping[str, Iterable[str]], language: str | None = None) -> str:
    """Render the skeleton. Every mandatory section is present; empty ones carry a marker."""
    unknown = set(sections) - set(SECTION_KEYS)
    if unknown:
        raise ValueError(f"unknown section keys: {sorted(unknown)}")
    out: list[str] = [f"# {title}", ""]
    for key, _heading in SECTIONS:
        out.append(f"## {heading_for(key, language)}")
        out.append("")
        lines = [line.rstrip() for line in sections.get(key, [])]
        out.extend(lines if lines else [EMPTY_MARKER])
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def headings_of(markdown: str) -> list[str]:
    return [line[3:].strip() for line in markdown.splitlines() if line.startswith("## ")]
