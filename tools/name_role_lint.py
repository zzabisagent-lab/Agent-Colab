"""Product-name consistency (V-P0-01) and fixed-role neutrality (V-P0-02) lint.

V-P0-01: every user-facing occurrence of the product name is spelled ``Agent-Colab``; variants
such as ``Agent Colab``, ``AgentColab`` or ``agent colab`` are rejected in docs, UI, metadata,
policy, and server code (code identifiers ``agent_colab``/``agent-colab`` are allowed).

V-P0-02: no specific Agent product or machine is hard-coded as a core role in schema, policy, or
server code. Product names may appear only in the allow-listed places (baseline documents,
pipeline tooling that names the implementer/verifier of *this repository's* development process,
spike notes, and test fixtures that deliberately use them as ordinary labels).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from tools.baseline import ROOT

NAME_VARIANTS = re.compile(r"\b(Agent Colab|AgentColab|agent colab|Agent-colab|agent-Colab)\b")
PRODUCT_NAMES = re.compile(
    r"\b(claude|codex|openai|anthropic|gpt-?[0-9o]|gemini|copilot|llama|mistral|cursor)\b", re.I
)
SCAN_NAME = [
    "README.md",
    "AGENTS.md",
    "PROGRESS.md",
    "pyproject.toml",
    "docs",
    "server",
    "policy",
    "schemas",
    "web-admin/index.html",
    "web-admin/src",
    "compose.yaml",
    "deploy",
    "i18n",
]
SCAN_CORE = ["server", "policy", "schemas", "migrations", "compose.yaml", "deploy", "web-admin/src"]
ALLOW_PRODUCT = (
    "docs/baseline/",
    "docs/adr/ADR-0003",
    "docs/adr/ADR-0005",
    "docs/protocol/",
    "docs/security/",
    "docs/plan-baseline.md",
    "server/schedules/",  # spike/reference comments only; checked below for role usage
)
SKIP_DIRS = {"node_modules", ".venv", "dist", "__pycache__"}


def _files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        path = ROOT / p
        if path.is_file():
            out.append(path)
        elif path.is_dir():
            out.extend(
                f
                for f in path.rglob("*")
                if f.is_file()
                and not (set(f.parts) & SKIP_DIRS)
                and f.suffix
                in {".md", ".py", ".yaml", ".yml", ".json", ".toml", ".html", ".ts", ".tsx"}
            )
    return out


def main() -> int:
    problems: list[str] = []
    for f in _files(SCAN_NAME):
        if "docs/baseline" in str(f):
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if NAME_VARIANTS.search(line):
                problems.append(f"NAME {f.relative_to(ROOT)}:{i}: {line.strip()[:100]}")
    for f in _files(SCAN_CORE):
        rel = str(f.relative_to(ROOT))
        if rel.startswith(ALLOW_PRODUCT):
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if PRODUCT_NAMES.search(line):
                problems.append(f"ROLE {rel}:{i}: {line.strip()[:100]}")
    for p in problems:
        print(p)
    print(f"name_role_lint: {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
