"""Bridge redaction scanner (spec §10.2 redaction, §15.7, §21.1 DLP scope).

Only the redacted form of a relayed message may be persisted or forwarded. The scanner reports
finding *kinds*, never the matched values. P2-15 provides the general ingestion scanner; this
module is the Bridge's local boundary and keeps the same output contract.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("canary", re.compile(r"CANARY-NOT-A-SECRET-\d+")),
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    ),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("telegram_bot_token", re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")),
    ("dsn_credentials", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s@]+@[^\s]+")),
    (
        "password_assignment",
        re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|token)\s*[=:]\s*\S{6,}"),
    ),
)
_HIGH_ENTROPY = re.compile(r"\b[A-Za-z0-9+/=_-]{40,}\b")


def _entropy(value: str) -> float:
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    total = len(value)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


@dataclass(frozen=True)
class RedactionResult:
    text: str
    findings: tuple[str, ...]  # kinds only, in order of first occurrence

    @property
    def redacted(self) -> bool:
        return bool(self.findings)


def redact(text: str) -> RedactionResult:
    """Replace secret-looking spans with ``<redacted:kind>``; return kinds, never values."""
    findings: list[str] = []
    out = text
    for kind, pattern in _PATTERNS:
        if pattern.search(out):
            findings.append(kind)
            out = pattern.sub(f"<redacted:{kind}>", out)

    def _high(m: re.Match[str]) -> str:
        token = m.group(0)
        if _entropy(token) >= 4.0 and not token.startswith("<redacted"):
            if "high_entropy" not in findings:
                findings.append("high_entropy")
            return "<redacted:high_entropy>"
        return token

    out = _HIGH_ENTROPY.sub(_high, out)
    return RedactionResult(out, tuple(findings))
