"""Publisher contract (development plan §10.3; P6-06).

```text
publish(document, manifest, destination) -> external_ref/version
update(document_id, new_version)         -> external_ref/version
verify(external_ref, checksum)           -> result
archive(external_ref)                    -> result
```

A publisher moves canonical Markdown plus its JSON manifest to a destination and can prove
afterwards that what is there still matches the canonical checksum. Publishers are registered by
kind, so a new destination type is a registration rather than a change to the publishing command.

Destination configuration never holds a secret value: a credential is a Secret Broker reference
(``credential_ref``) that the caller resolves and passes as ``token`` at construction time.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

PUBLISH_ERROR_CODES: tuple[str, ...] = (
    "PUBLISH_DESTINATION_UNAVAILABLE",
    "PUBLISH_DESTINATION_INVALID",
    "PUBLISH_AUTH_FAILED",
    "PUBLISH_CHECKSUM_MISMATCH",
    "PUBLISH_NOT_FOUND",
    "PUBLISH_REJECTED",
)


class PublishError(Exception):
    """Stable, value-free publisher failure."""

    def __init__(self, code: str, detail: str = "", retryable: bool = False) -> None:
        if code not in PUBLISH_ERROR_CODES:
            raise ValueError(f"unknown publish error code {code}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.retryable = retryable


@dataclass(frozen=True)
class PublishTarget:
    """One canonical document version handed to a publisher."""

    workspace_id: str
    document_id: str
    version: int
    markdown: str
    manifest: Mapping[str, Any]
    checksum: str  # SHA-256 of the canonical Markdown
    title: str = ""


@dataclass(frozen=True)
class PublishRecord:
    """Where the version landed and how to find it again."""

    external_ref: str
    external_version: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    checksum: str | None = None
    detail: str = ""


@runtime_checkable
class Publisher(Protocol):
    kind: str

    def publish(self, target: PublishTarget) -> PublishRecord: ...

    def update(self, target: PublishTarget) -> PublishRecord: ...

    def verify(self, external_ref: str, checksum: str) -> VerifyResult: ...

    def archive(self, external_ref: str) -> PublishRecord: ...


PublisherFactory = Callable[[Mapping[str, Any]], Publisher]
_PUBLISHERS: dict[str, PublisherFactory] = {}


def register_publisher(kind: str, factory: PublisherFactory, *, replace: bool = False) -> None:
    if kind in _PUBLISHERS and not replace:
        raise ValueError(f"publisher kind {kind!r} already registered")
    _PUBLISHERS[kind] = factory


def publisher_kinds() -> tuple[str, ...]:
    _load_builtins()
    return tuple(sorted(_PUBLISHERS))


def publisher_for(kind: str, config: Mapping[str, Any]) -> Publisher:
    _load_builtins()
    try:
        factory = _PUBLISHERS[kind]
    except KeyError as exc:
        raise PublishError("PUBLISH_DESTINATION_INVALID", f"unknown publisher kind {kind}") from exc
    return factory(config)


_LOADED = False


def _load_builtins() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    from server.documents.publishers import bookstack, filesystem, git

    del bookstack, filesystem, git


def canonical_files(target: PublishTarget) -> dict[str, str]:
    """The two files every destination receives: canonical Markdown and its JSON manifest."""
    import json

    return {
        f"{target.document_id}/v{target.version}.md": target.markdown,
        f"{target.document_id}/v{target.version}.json": json.dumps(
            dict(target.manifest), indent=2, ensure_ascii=False, sort_keys=True
        )
        + "\n",
    }
