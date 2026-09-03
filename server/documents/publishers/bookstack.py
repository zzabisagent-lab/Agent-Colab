"""BookStack reference connector (development plan §10.3; P6-06, V-P6-21).

BookStack's REST API is used as the reference wiki connector: a document version becomes a page
in a configured book, created on first publish and updated in place afterwards. Wiki.js would
implement the same :class:`~server.documents.publishers.base.Publisher` contract; only this class
would change, which is what "common adapter contract" means here.

The HTTP transport is injectable (``config["_transport"]``) so contract tests run offline against
a fake BookStack. The API token is a Secret Broker value resolved by the caller and passed as
``token``; it is never stored in the destination configuration.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

import httpx

from server.documents.publishers.base import (
    PublishError,
    PublishRecord,
    PublishTarget,
    VerifyResult,
    register_publisher,
)

TIMEOUT_S = 30.0


class BookStackPublisher:
    kind = "bookstack"

    def __init__(self, config: Mapping[str, Any]) -> None:
        base_url = str(config.get("base_url") or "").strip().rstrip("/")
        book_id = config.get("book_id")
        if not base_url or book_id is None:
            raise PublishError("PUBLISH_DESTINATION_INVALID", "base_url and book_id are required")
        self.base_url = base_url
        self.book_id = int(book_id)
        self._token = str(config.get("token") or "")  # resolved Secret Broker value, never stored
        self._transport = config.get("_transport")
        self.fail = bool(config.get("_fail", False))  # test seam for the outage case

    # ------------------------------------------------------------------ transport
    def _client(self) -> httpx.Client:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Token {self._token}"
        return httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=TIMEOUT_S,
            transport=self._transport,
        )

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if self.fail:
            raise PublishError("PUBLISH_DESTINATION_UNAVAILABLE", "BookStack is down", True)
        try:
            with self._client() as client:
                response = client.request(method, path, json=payload)
        except httpx.TimeoutException as exc:
            raise PublishError("PUBLISH_DESTINATION_UNAVAILABLE", "timeout", True) from exc
        except httpx.HTTPError as exc:
            raise PublishError("PUBLISH_DESTINATION_UNAVAILABLE", type(exc).__name__, True) from exc
        if response.status_code in (401, 403):
            raise PublishError("PUBLISH_AUTH_FAILED", f"status {response.status_code}")
        if response.status_code == 404:
            raise PublishError("PUBLISH_NOT_FOUND", path)
        if response.status_code >= 500:
            raise PublishError(
                "PUBLISH_DESTINATION_UNAVAILABLE", f"status {response.status_code}", True
            )
        if response.status_code >= 400:
            raise PublishError("PUBLISH_REJECTED", f"status {response.status_code}")
        if not response.content:
            return {}
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise PublishError("PUBLISH_REJECTED", "response is not JSON") from exc

    # ------------------------------------------------------------------ contract
    def _page_name(self, target: PublishTarget) -> str:
        """A wiki page represents the document, so its name is version-stable: publishing a new
        version updates that page in place and the version travels as a tag."""
        return target.document_id

    def _body(self, target: PublishTarget) -> dict[str, Any]:
        return {
            "book_id": self.book_id,
            "name": self._page_name(target),
            "markdown": target.markdown,
            "tags": [
                {"name": "title", "value": target.title or target.document_id},
                {"name": "document_id", "value": target.document_id},
                {"name": "version", "value": str(target.version)},
                {"name": "checksum", "value": target.checksum},
            ],
        }

    def publish(self, target: PublishTarget) -> PublishRecord:
        data = self._call("POST", "/api/pages", self._body(target))
        page_id = data.get("id")
        if page_id is None:
            raise PublishError("PUBLISH_REJECTED", "no page id in response")
        return PublishRecord(
            f"bookstack://{self.base_url}/api/pages/{page_id}",
            str(data.get("revision_count") or 1),
            {"page_id": page_id},
        )

    def update(self, target: PublishTarget) -> PublishRecord:
        data = self._call("POST", "/api/pages", self._body(target))
        page_id = data.get("id")
        return PublishRecord(
            f"bookstack://{self.base_url}/api/pages/{page_id}",
            str(data.get("revision_count") or 1),
            {"page_id": page_id},
        )

    def _page_path(self, external_ref: str) -> str:
        rest = external_ref.removeprefix("bookstack://")
        marker = "/api/pages/"
        if marker not in rest:
            raise PublishError("PUBLISH_DESTINATION_INVALID", "unsupported external ref")
        return marker + rest.split(marker, 1)[1]

    def verify(self, external_ref: str, checksum: str) -> VerifyResult:
        try:
            data = self._call("GET", self._page_path(external_ref))
        except PublishError as exc:
            if exc.code == "PUBLISH_NOT_FOUND":
                return VerifyResult(False, None, "missing at destination")
            raise
        markdown = str(data.get("markdown") or data.get("html") or "")
        actual = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        return VerifyResult(actual == checksum, actual, "" if actual == checksum else "mismatch")

    def archive(self, external_ref: str) -> PublishRecord:
        path = self._page_path(external_ref)
        self._call("DELETE", path)
        return PublishRecord(external_ref, None, {"archived": True})


register_publisher("bookstack", BookStackPublisher)
