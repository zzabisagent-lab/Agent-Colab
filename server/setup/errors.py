"""Stable Setup error codes (development plan §7.1 Problem Details + stable codes)."""

from __future__ import annotations


class SetupError(Exception):
    """Raised by the Setup contract with a stable ``code`` and a redacted ``detail``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
