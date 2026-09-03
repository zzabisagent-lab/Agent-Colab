"""V-P6-21 (P6-06): the BookStack reference connector satisfies the Publisher contract and the
content it stores matches the canonical Markdown. The HTTP transport is a fake BookStack, so the
contract is exercised offline; a Wiki.js connector would implement the same protocol."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest

from server.documents.publishers.base import (
    Publisher,
    PublishError,
    PublishTarget,
    publisher_for,
    publisher_kinds,
)

TOKEN = "secret-token-value"
MARKDOWN = "# Canonical\n\nThe body that must arrive unchanged.\n"
CHECKSUM = hashlib.sha256(MARKDOWN.encode("utf-8")).hexdigest()


class FakeBookStack:
    """Minimal BookStack API: create/update a page, read it back, delete it."""

    def __init__(self) -> None:
        self.pages: dict[int, dict[str, Any]] = {}
        self.next_id = 1
        self.calls: list[tuple[str, str]] = []
        self.status_override: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if self.status_override is not None:
            return httpx.Response(self.status_override, json={"error": "forced"})
        if request.headers.get("Authorization") != f"Token {TOKEN}":
            return httpx.Response(401, json={"error": "unauthorized"})
        if request.method == "POST" and request.url.path == "/api/pages":
            body = json.loads(request.content)
            name = str(body["name"])
            existing = next((p for p in self.pages.values() if p["name"] == name), None)
            if existing is None:
                page = {
                    "id": self.next_id,
                    "name": name,
                    "markdown": body["markdown"],
                    "tags": body.get("tags", []),
                    "revision_count": 1,
                }
                self.pages[self.next_id] = page
                self.next_id += 1
            else:
                existing.update(markdown=body["markdown"], tags=body.get("tags", []))
                existing["revision_count"] += 1
                page = existing
            return httpx.Response(200, json=page)
        if request.url.path.startswith("/api/pages/"):
            page_id = int(request.url.path.rsplit("/", 1)[1])
            page = self.pages.get(page_id)
            if page is None:
                return httpx.Response(404, json={"error": "not found"})
            if request.method == "DELETE":
                self.pages.pop(page_id)
                return httpx.Response(204)
            return httpx.Response(200, json=page)
        return httpx.Response(404, json={"error": "unknown path"})


def _publisher(fake: FakeBookStack, token: str = TOKEN) -> Publisher:
    return publisher_for(
        "bookstack",
        {
            "base_url": "https://wiki.example.test",
            "book_id": 7,
            "token": token,
            "_transport": httpx.MockTransport(fake.handler),
        },
    )


def _target(version: int = 1, markdown: str = MARKDOWN) -> PublishTarget:
    return PublishTarget(
        workspace_id="ws-connector",
        document_id="doc-connector",
        version=version,
        markdown=markdown,
        manifest={"document_id": "doc-connector", "version": version},
        checksum=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        title=f"doc-connector v{version}",
    )


def test_connector_is_registered_under_the_common_contract() -> None:
    assert "bookstack" in publisher_kinds()
    assert {"filesystem", "git"} <= set(publisher_kinds())
    fake = FakeBookStack()
    publisher = _publisher(fake)
    assert isinstance(publisher, Publisher)  # structural check against the protocol


def test_publish_update_verify_archive_round_trip() -> None:
    fake = FakeBookStack()
    publisher = _publisher(fake)
    record = publisher.publish(_target())
    assert record.external_ref.endswith("/api/pages/1")
    # what the destination holds is byte-for-byte the canonical Markdown
    assert fake.pages[1]["markdown"] == MARKDOWN
    assert publisher.verify(record.external_ref, CHECKSUM).ok

    corrected = "# Canonical\n\nThe body after a correction.\n"
    updated = publisher.update(_target(version=2, markdown=corrected))
    assert updated.external_version == "2"  # a revision, not a second page
    assert fake.pages[1]["markdown"] == corrected
    stale = publisher.verify(updated.external_ref, CHECKSUM)
    assert not stale.ok and stale.checksum == hashlib.sha256(corrected.encode()).hexdigest()

    publisher.archive(updated.external_ref)
    assert fake.pages == {}
    gone = publisher.verify(updated.external_ref, CHECKSUM)
    assert not gone.ok and gone.detail == "missing at destination"


def test_connector_maps_failures_to_stable_codes() -> None:
    fake = FakeBookStack()
    with pytest.raises(PublishError) as auth:
        _publisher(fake, token="wrong-token").publish(_target())
    assert auth.value.code == "PUBLISH_AUTH_FAILED"

    fake.status_override = 503
    with pytest.raises(PublishError) as down:
        _publisher(fake).publish(_target())
    assert down.value.code == "PUBLISH_DESTINATION_UNAVAILABLE" and down.value.retryable

    fake.status_override = 422
    with pytest.raises(PublishError) as rejected:
        _publisher(fake).publish(_target())
    assert rejected.value.code == "PUBLISH_REJECTED"


def test_connector_requires_its_destination_configuration() -> None:
    with pytest.raises(PublishError) as exc:
        publisher_for("bookstack", {"base_url": "https://wiki.example.test"})
    assert exc.value.code == "PUBLISH_DESTINATION_INVALID"
    with pytest.raises(PublishError) as unknown:
        publisher_for("confluence", {})
    assert unknown.value.code == "PUBLISH_DESTINATION_INVALID"
