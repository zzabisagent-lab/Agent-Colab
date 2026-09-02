"""Service-token resolution. The implementation lives in ``server.identity.principals`` (P1-05);
this module keeps the Phase 0 import surface.
"""

from __future__ import annotations

from server.identity.principals import (
    Principal,
    issue_service_token,
    resolve_service_token,
    revoke_service_token,
    rotate_service_token,
    token_hash,
)

__all__ = [
    "Principal",
    "issue_service_token",
    "resolve_service_token",
    "revoke_service_token",
    "rotate_service_token",
    "token_hash",
]
