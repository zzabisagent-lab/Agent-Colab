"""Setup transport boundary (spec §12, development plan §8.1) — P0-09.

``/setup`` is reachable from loopback by default. A remote request is allowed only when ALL of
HTTPS/TLS reverse proxy, client mTLS, IP allowlist, and a valid setup token hold. The function is
pure: every other combination yields a stable denial code and no side effect (V-P4-27).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

CHECK_PASSED = "OK"  # value of token_check when SetupTokenGuard.verify succeeded


@dataclass(frozen=True)
class TransportRequest:
    bind_is_loopback: bool
    remote_addr: str
    tls_terminated_by_proxy: bool
    client_mtls_verified: bool
    allowlist: tuple[str, ...]
    token_check: str  # CHECK_PASSED or a SETUP_TOKEN_* code from SetupTokenGuard


@dataclass(frozen=True)
class TransportDecision:
    allowed: bool
    code: str
    origin: str  # "loopback" | "remote"


def _is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def _allowlisted(addr: str, allowlist: tuple[str, ...]) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    for entry in allowlist:
        try:
            if ip in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def evaluate_transport(request: TransportRequest) -> TransportDecision:
    if request.bind_is_loopback and _is_loopback(request.remote_addr):
        return TransportDecision(True, "SETUP_TRANSPORT_LOCAL", "loopback")
    if not request.tls_terminated_by_proxy:
        return TransportDecision(False, "SETUP_REMOTE_TLS_REQUIRED", "remote")
    if not request.client_mtls_verified:
        return TransportDecision(False, "SETUP_REMOTE_MTLS_REQUIRED", "remote")
    if not _allowlisted(request.remote_addr, request.allowlist):
        return TransportDecision(False, "SETUP_REMOTE_NOT_ALLOWLISTED", "remote")
    if request.token_check != CHECK_PASSED:
        code = (
            request.token_check
            if request.token_check.startswith("SETUP_TOKEN_")
            else "SETUP_TOKEN_INVALID"
        )
        return TransportDecision(False, code, "remote")
    return TransportDecision(True, "SETUP_TRANSPORT_REMOTE_VERIFIED", "remote")
