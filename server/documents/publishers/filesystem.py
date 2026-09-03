"""Filesystem/NAS publisher (development plan §10.3; P6-06).

Writes the canonical Markdown and its manifest under ``root/<workspace>/<document>/v<n>.*`` with
a temp-file rename so a reader never sees a half-written document, then verifies by re-hashing
what is on disk. Archiving moves the pair under ``_archive/`` and keeps the bytes.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from server.documents.publishers.base import (
    PublishError,
    PublishRecord,
    PublishTarget,
    VerifyResult,
    canonical_files,
    register_publisher,
)

SCHEME = "colab-file"


class FilesystemPublisher:
    kind = "filesystem"

    def __init__(self, config: Mapping[str, Any]) -> None:
        root = str(config.get("root") or "").strip()
        if not root:
            raise PublishError("PUBLISH_DESTINATION_INVALID", "root is required")
        self.root = Path(root)
        self.fail = bool(config.get("_fail", False))  # test seam for the outage case

    # ------------------------------------------------------------------ helpers
    def _dir(self, target: PublishTarget) -> Path:
        return self.root / target.workspace_id / target.document_id

    def _ref(self, target: PublishTarget) -> str:
        return f"{SCHEME}://{target.workspace_id}/{target.document_id}/v{target.version}"

    def _paths(self, external_ref: str) -> tuple[Path, Path]:
        rest = external_ref.removeprefix(f"{SCHEME}://")
        parts = rest.split("/")
        if len(parts) != 3 or not parts[2].startswith("v"):
            raise PublishError("PUBLISH_DESTINATION_INVALID", "unsupported external ref")
        ws, doc, ver = parts
        base = self.root / ws / doc
        return base / f"{ver}.md", base / f"{ver}.json"

    def _write(self, target: PublishTarget) -> PublishRecord:
        if self.fail:
            raise PublishError("PUBLISH_DESTINATION_UNAVAILABLE", "destination is down", True)
        directory = self._dir(target)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            for name, content in canonical_files(target).items():
                path = self.root / target.workspace_id / name
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(content, encoding="utf-8")
                os.replace(tmp, path)
        except OSError as exc:
            raise PublishError("PUBLISH_DESTINATION_UNAVAILABLE", type(exc).__name__, True) from exc
        return PublishRecord(self._ref(target), str(target.version), {"root": str(self.root)})

    # ------------------------------------------------------------------ contract
    def publish(self, target: PublishTarget) -> PublishRecord:
        return self._write(target)

    def update(self, target: PublishTarget) -> PublishRecord:
        return self._write(target)

    def verify(self, external_ref: str, checksum: str) -> VerifyResult:
        md_path, _manifest = self._paths(external_ref)
        if not md_path.exists():
            return VerifyResult(False, None, "missing at destination")
        actual = hashlib.sha256(md_path.read_bytes()).hexdigest()
        return VerifyResult(actual == checksum, actual, "" if actual == checksum else "mismatch")

    def archive(self, external_ref: str) -> PublishRecord:
        md_path, manifest_path = self._paths(external_ref)
        if not md_path.exists():
            raise PublishError("PUBLISH_NOT_FOUND", "nothing published at that ref")
        archive_dir = md_path.parent / "_archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        for path in (md_path, manifest_path):
            if path.exists():
                shutil.move(str(path), str(archive_dir / path.name))
        return PublishRecord(external_ref, None, {"archived_to": str(archive_dir)})


register_publisher("filesystem", FilesystemPublisher)
