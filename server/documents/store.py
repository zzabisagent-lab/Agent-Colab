"""Canonical document storage: Markdown + JSON manifest, write-once per version
(spec §14.2; ``<root>/<workspace>/<document_id>/v<version>.md|.json``)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_ROOT = "/var/lib/agent-colab/documents"
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class DocumentStoreError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def document_root() -> Path:
    return Path(os.environ.get("AGENT_COLAB_DOCUMENT_ROOT", DEFAULT_ROOT))


@dataclass(frozen=True)
class StoredVersion:
    storage_uri: str
    markdown_path: Path
    manifest_path: Path


class DocumentStore:
    def __init__(self, root: Path | None = None) -> None:
        self._root = root or document_root()

    @property
    def root(self) -> Path:
        return self._root

    def _dir(self, workspace_id: str, document_id: str) -> Path:
        for part in (workspace_id, document_id):
            if not _SAFE.match(part):
                raise DocumentStoreError("DOCUMENT_PATH_INVALID", part)
        return self._root / workspace_id / document_id

    def uri(self, workspace_id: str, document_id: str, version: int) -> str:
        return f"colab-doc://{workspace_id}/{document_id}/v{version}"

    def write_version(
        self,
        workspace_id: str,
        document_id: str,
        version: int,
        markdown: str,
        manifest: dict[str, Any],
    ) -> StoredVersion:
        """Write both files atomically-ish (temp + rename); an existing version is never touched."""
        d = self._dir(workspace_id, document_id)
        d.mkdir(parents=True, exist_ok=True)
        md_path = d / f"v{version}.md"
        mf_path = d / f"v{version}.json"
        if md_path.exists() or mf_path.exists():
            raise DocumentStoreError("DOCUMENT_VERSION_EXISTS", f"{document_id} v{version}")
        for path, content in (
            (md_path, markdown),
            (mf_path, json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n"),
        ):
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
            path.chmod(0o444)
        return StoredVersion(self.uri(workspace_id, document_id, version), md_path, mf_path)

    def read_version(
        self, workspace_id: str, document_id: str, version: int
    ) -> tuple[str, dict[str, Any]]:
        d = self._dir(workspace_id, document_id)
        md_path, mf_path = d / f"v{version}.md", d / f"v{version}.json"
        if not md_path.exists():
            raise DocumentStoreError("DOCUMENT_VERSION_MISSING", f"{document_id} v{version}")
        manifest: dict[str, Any] = json.loads(mf_path.read_text(encoding="utf-8"))
        return md_path.read_text(encoding="utf-8"), manifest
