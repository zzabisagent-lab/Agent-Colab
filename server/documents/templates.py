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


def render(title: str, sections: Mapping[str, Iterable[str]]) -> str:
    """Render the skeleton. Every mandatory section is present; empty ones carry a marker."""
    unknown = set(sections) - set(SECTION_KEYS)
    if unknown:
        raise ValueError(f"unknown section keys: {sorted(unknown)}")
    out: list[str] = [f"# {title}", ""]
    for key, heading in SECTIONS:
        out.append(f"## {heading}")
        out.append("")
        lines = [line.rstrip() for line in sections.get(key, [])]
        out.extend(lines if lines else [EMPTY_MARKER])
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def headings_of(markdown: str) -> list[str]:
    return [line[3:].strip() for line in markdown.splitlines() if line.startswith("## ")]
