"""Logging that can only ever mention handle/lease ids and outcomes.

The sidecar's own log calls carry ids and result codes only. :class:`SafeLogFilter` is a second
line of defense for third-party log lines: token-like strings, ``key=value`` pairs for secret-ish
keys, lengths and long hex digests are redacted before a record is emitted.
"""

from __future__ import annotations

import logging
import re

REDACTED = "[redacted]"
_ID = re.compile(r"\b(?:sh|sc|wi|lease|ls|task|agent)-[0-9a-f-]{6,64}\b")
_KV = re.compile(
    r"(?i)\b(secret(?:_b64)?|value|token|password|passwd|authorization|key)\b"
    r"(\s*[:=]\s*)([^\s,;\"']+)"
)
_BEARER = re.compile(r"(?i)\b(bearer)(\s+)([^\s,;\"']+)")
_TOKENISH = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{20,}(?![A-Za-z0-9+/=_-])")
_LENGTH = re.compile(r"(?i)\b(len(?:gth)?|size|bytes|n_bytes)\s*[:=]\s*\d+")
_HEX = re.compile(r"\b[0-9a-f]{32,}\b")


def redact(text: str) -> str:
    """Redact anything that could be a secret value, its length or a digest of it."""
    keep: list[str] = []

    def _protect(match: re.Match[str]) -> str:
        keep.append(match.group(0))
        return f"\x00{len(keep) - 1}\x00"

    protected = _ID.sub(_protect, text)
    protected = _KV.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", protected)
    protected = _BEARER.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", protected)
    protected = _LENGTH.sub(lambda m: f"{m.group(1)}={REDACTED}", protected)
    protected = _HEX.sub(REDACTED, protected)
    protected = _TOKENISH.sub(REDACTED, protected)
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], protected)


class SafeLogFilter(logging.Filter):
    """Install on every handler: ``logging.getLogger().handlers[i].addFilter(SafeLogFilter())``."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a bad format string must not leak the args either
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """Root logging with the safe filter on a stderr handler (idempotent)."""
    root = logging.getLogger()
    if not any(isinstance(f, SafeLogFilter) for h in root.handlers for f in h.filters):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        handler.addFilter(SafeLogFilter())
        root.addHandler(handler)
    root.setLevel(level)
    return root
