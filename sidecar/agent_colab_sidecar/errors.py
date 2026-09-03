"""Stable, value-free sidecar errors."""

from __future__ import annotations

BROKER_DENIAL_CODES = (
    "SECRET_NOT_FOUND",
    "SECRET_SCOPE_DENIED",
    "SECRET_LEASE_EXPIRED",
    "SECRET_HANDLE_USED",
    "SECRET_HANDLE_REVOKED",
    "SECRET_HANDLE_HOST_MISMATCH",
    "SECRET_EXPOSURE_APPROVAL_REQUIRED",
)
SIDECAR_CODES = (
    *BROKER_DENIAL_CODES,
    "BROKER_UNAVAILABLE",
    "BROKER_AUTH_FAILED",
    "BROKER_BAD_RESPONSE",
    "LEASE_UNKNOWN",
    "LEASE_REVOKED",
    "INJECTION_FAILED",
    "CONFIG_INVALID",
)


class SidecarError(Exception):
    """Carries a stable code and a detail that never contains a value, length or hash."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in SIDECAR_CODES:
            raise ValueError(f"unknown sidecar error code {code}")
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
