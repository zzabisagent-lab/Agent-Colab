"""Signing-key resolution for REST/Webhook push (development plan §7B.2).

The Agent record stores only a Secret Broker *reference* (``agents.credential_ref``); the key
bytes are resolved at send/verify time and never persisted, logged or returned. This module is
the Phase 4 (P4-05) Secret Broker seam: ``EnvSigningKeyResolver`` reads
``AGENT_COLAB_WEBHOOK_KEY_<REF>`` (reference upper-cased, ``-``/``.``/``@`` → ``_``) until the
broker replaces it behind the same ``SigningKeyResolver`` protocol.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Protocol

from server.agents.adapters.contract import AdapterError

KEY_REF_PATTERN = re.compile(r"^sec-[A-Za-z0-9._-]{1,64}(@v[0-9]+)?$")


class SigningKeyResolver(Protocol):
    def resolve(self, key_ref: str) -> bytes:
        """Key bytes for ``key_ref``; raises ``AdapterError('ADAPTER_AUTH_FAILED')`` if absent."""
        ...


def env_name_for(key_ref: str) -> str:
    if not KEY_REF_PATTERN.match(key_ref):
        raise AdapterError("ADAPTER_AUTH_FAILED", "signing key reference is not well-formed")
    return "AGENT_COLAB_WEBHOOK_KEY_" + re.sub(r"[^A-Za-z0-9]", "_", key_ref).upper()


class EnvSigningKeyResolver:
    """Development resolver: reference → environment variable (never echoes the value)."""

    def resolve(self, key_ref: str) -> bytes:
        value = os.environ.get(env_name_for(key_ref))
        if not value:
            raise AdapterError("ADAPTER_AUTH_FAILED", f"no signing key for reference {key_ref}")
        return value.encode("utf-8")


class StaticSigningKeyResolver:
    """Test resolver over an in-memory mapping."""

    def __init__(self, keys: Mapping[str, bytes]) -> None:
        self._keys = dict(keys)

    def resolve(self, key_ref: str) -> bytes:
        try:
            return self._keys[key_ref]
        except KeyError as exc:
            raise AdapterError("ADAPTER_AUTH_FAILED", f"no signing key for {key_ref}") from exc


_DEFAULT: SigningKeyResolver | None = None


def default_resolver() -> SigningKeyResolver:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = EnvSigningKeyResolver()
    return _DEFAULT


def set_default_resolver(resolver: SigningKeyResolver | None) -> None:
    global _DEFAULT
    _DEFAULT = resolver
