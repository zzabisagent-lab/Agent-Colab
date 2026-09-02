"""Implementer/verifier independence (validation plan §4.1, development plan §6.4).

Pure application check executed before any VerificationRun insert. The DB CHECK constraints in
migration 0001 are the second line of defence; both must reject (V-P0-07).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


class VerificationIndependenceError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Identity:
    account_id: str
    credential_fingerprint: str
    agent_id: str | None = None


def effective_principal(account_id: str, alias_graph: Mapping[str, str]) -> str:
    """Follow alias edges (account -> canonical account) to the root; cycles resolve to the min."""
    seen: list[str] = []
    current = account_id
    while current in alias_graph and current not in seen:
        seen.append(current)
        current = alias_graph[current]
    if current in seen:  # cycle: pick a deterministic representative
        return min(seen)
    return current


def check_independence(
    implementer: Identity,
    verifier: Identity,
    alias_graph: Mapping[str, str] | None = None,
    verifier_permissions: frozenset[str] | None = None,
    commit_author_account_id: str | None = None,
) -> None:
    graph = alias_graph or {}
    if implementer.account_id == verifier.account_id:
        raise VerificationIndependenceError("VERIFIER_SAME_ACCOUNT", implementer.account_id)
    if implementer.agent_id and verifier.agent_id and implementer.agent_id == verifier.agent_id:
        raise VerificationIndependenceError("VERIFIER_SAME_AGENT", implementer.agent_id)
    if implementer.credential_fingerprint == verifier.credential_fingerprint:
        raise VerificationIndependenceError("VERIFIER_SAME_CREDENTIAL", "shared fingerprint")
    if effective_principal(implementer.account_id, graph) == effective_principal(
        verifier.account_id, graph
    ):
        raise VerificationIndependenceError("VERIFIER_ALIAS_OF_IMPLEMENTER", verifier.account_id)
    if commit_author_account_id is not None and commit_author_account_id == verifier.account_id:
        raise VerificationIndependenceError("VERIFIER_IS_COMMIT_AUTHOR", verifier.account_id)
    if verifier_permissions is not None and "verification.submit" not in verifier_permissions:
        raise VerificationIndependenceError("VERIFIER_NOT_ELIGIBLE", "verification.submit missing")
