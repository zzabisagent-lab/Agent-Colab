"""OIDC adapter interface (P4-09, optional). Not enabled by default.

An external provider implements :class:`OidcProvider`; ``provider_from_env`` returns None unless
``AGENT_COLAB_OIDC_PROVIDER`` names a registered provider. A successful code exchange counts as an
MFA proof (method ``oidc``) only when the provider asserts ``amr`` containing ``mfa``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class OidcIdentity:
    subject: str
    email: str | None
    amr: tuple[str, ...] = ()
    claims: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class OidcProvider(Protocol):
    name: str

    def authorization_url(self, state: str, nonce: str, redirect_uri: str) -> str: ...

    def exchange_code(self, code: str, nonce: str, redirect_uri: str) -> OidcIdentity: ...

    def validate_id_token(self, id_token: str, nonce: str) -> OidcIdentity: ...  # JWKS-backed


_PROVIDERS: dict[str, Callable[[Mapping[str, Any]], OidcProvider]] = {}


def register_provider(name: str, factory: Callable[[Mapping[str, Any]], OidcProvider]) -> None:
    _PROVIDERS[name] = factory


def enabled() -> bool:
    return bool(os.environ.get("AGENT_COLAB_OIDC_PROVIDER"))


def provider_from_env(config: Mapping[str, Any] | None = None) -> OidcProvider | None:
    name = os.environ.get("AGENT_COLAB_OIDC_PROVIDER")
    if not name:
        return None
    try:
        return _PROVIDERS[name](config or {})
    except KeyError as exc:
        raise ValueError(f"OIDC provider {name!r} not registered") from exc


@dataclass
class FakeOidcProvider:
    """Test double: deterministic identities keyed by code; ``amr`` decides the MFA proof."""

    name: str = "fake"
    identities: dict[str, OidcIdentity] = field(default_factory=dict)

    def authorization_url(self, state: str, nonce: str, redirect_uri: str) -> str:
        return f"https://idp.example.test/authorize?state={state}&nonce={nonce}&redirect_uri={redirect_uri}"

    def exchange_code(self, code: str, nonce: str, redirect_uri: str) -> OidcIdentity:
        try:
            return self.identities[code]
        except KeyError as exc:
            raise ValueError("OIDC_CODE_INVALID") from exc

    def validate_id_token(self, id_token: str, nonce: str) -> OidcIdentity:
        return self.exchange_code(id_token, nonce, "")
