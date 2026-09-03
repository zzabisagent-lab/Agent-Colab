"""Redaction pass of the documentation pipeline (development plan §10.1 ``REDACT``, §9.3).

Canonical and published documents must contain zero secret canaries and no personal data that a
source happened to carry (V-P6-13). The pass is a pure, deterministic text transform so the
hash-reproducibility of layer 1 survives it: the same freeze always redacts to the same bytes.

What is recorded is the *count* per rule and a salted SHA-256 of the matched text — never the
value, its length or its plaintext hash, so the ledger cannot be used to recover a secret.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

# A stable, public salt: the sample hash is a correlation aid between versions, never a lookup
# key for the value. Rainbow-table resistance comes from the salt plus the rule name.
SAMPLE_SALT = b"agent-colab/document-redaction/v1"

MARKERS: dict[str, str] = {
    "canary": "[redacted: secret]",
    "token": "[redacted: secret]",  # nosec B105 - a redaction marker, not a value
    "email": "[redacted: email]",
    "phone": "[redacted: phone]",
    "card": "[redacted: card]",
}

# Ordered: the most specific rule wins, so a canary is never reported as a generic token.
RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("canary", re.compile(r"CANARY-NOT-A-SECRET-\d{4}")),
    (
        "email",
        re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    ),
    # a card candidate must not be embedded in a longer token (a hex hash often holds a long
    # digit run) and must pass the Luhn check, so hashes and ids are never mistaken for cards
    ("card", re.compile(r"(?<![0-9A-Za-z])(?:\d[ -]?){12,18}\d(?![0-9A-Za-z])")),
    ("phone", re.compile(r"(?<![\w+])\+\d{1,3}[\s-]?\d{2,4}[\s-]?\d{3,4}[\s-]?\d{3,4}(?!\d)")),
    # provider-shaped bearer material that must never reach a document
    ("token", re.compile(r"\b(?:xox[baprs]-[A-Za-z0-9-]{10,}|gh[pousr]_[A-Za-z0-9]{20,})\b")),
)


@dataclass(frozen=True)
class RedactionCount:
    rule: str
    count: int
    sample_hash: str  # salted hash of the first match — never the value


def _sample_hash(rule: str, matched: str) -> str:
    return hashlib.sha256(SAMPLE_SALT + rule.encode() + b"|" + matched.encode()).hexdigest()


def _luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _accepts(rule: str, matched: str) -> bool:
    """Second-stage check that keeps a rule from firing on look-alike text."""
    if rule != "card":
        return True
    digits = re.sub(r"[^0-9]", "", matched)
    return 13 <= len(digits) <= 19 and _luhn(digits)


def redact(markdown: str) -> tuple[str, list[RedactionCount]]:
    """Return the redacted text and one count per rule that matched (deterministic)."""
    out = markdown
    counts: list[RedactionCount] = []
    for rule, pattern in RULES:
        matches = [m for m in pattern.findall(out) if _accepts(rule, str(m))]
        if not matches:
            continue
        first = str(matches[0])

        def _replace(match: re.Match[str], rule_name: str = rule) -> str:
            return MARKERS[rule_name] if _accepts(rule_name, match.group(0)) else match.group(0)

        out = pattern.sub(_replace, out)
        counts.append(RedactionCount(rule, len(matches), _sample_hash(rule, first)))
    return out, counts


def scan_only(markdown: str) -> list[RedactionCount]:
    """Counts without rewriting: used to assert that a stored version is already clean."""
    return redact(markdown)[1]


def as_manifest(counts: Iterable[RedactionCount]) -> list[dict[str, object]]:
    return [{"rule": c.rule, "count": c.count, "sample_hash": c.sample_hash} for c in counts]


def record(
    session: Session, document_id: str, version: int, counts: Iterable[RedactionCount]
) -> int:
    """Persist the per-rule counts of one version (idempotent per (document, version, rule))."""
    written = 0
    for c in counts:
        session.execute(
            text(
                "INSERT INTO document_redactions (document_id, version, rule, count, sample_hash) "
                "VALUES (:d, :v, :r, :c, :s) ON CONFLICT (document_id, version, rule) DO NOTHING"
            ),
            {"d": document_id, "v": version, "r": c.rule, "c": c.count, "s": c.sample_hash},
        )
        written += 1
    return written


def counts_for(session: Session, document_id: str, version: int) -> list[RedactionCount]:
    rows = session.execute(
        text(
            "SELECT rule, count, sample_hash FROM document_redactions "
            "WHERE document_id = :d AND version = :v ORDER BY rule"
        ),
        {"d": document_id, "v": version},
    ).all()
    return [RedactionCount(str(r[0]), int(r[1]), str(r[2])) for r in rows]


__all__ = [
    "MARKERS",
    "RULES",
    "RedactionCount",
    "as_manifest",
    "counts_for",
    "record",
    "redact",
    "scan_only",
]
