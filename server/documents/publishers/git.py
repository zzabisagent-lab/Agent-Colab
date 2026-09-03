"""Git-compatible publisher (development plan §10.3; P6-06, V-P6-15).

Commits the canonical Markdown and manifest into a working clone and pushes to the configured
remote (a Gitea or any Git remote; a local bare repository is enough for tests). ``git`` is
driven through ``subprocess`` so no dependency is added. ``verify`` re-reads the file from the
remote's committed tree, so a checksum answer reflects what the destination actually holds.

External refs are ``git://<commit sha>/<path>``, which pins both the version and its content.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess  # nosec B404 - git is driven with fixed argument lists, never a shell
from collections.abc import Mapping, Sequence
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

GIT = shutil.which("git")  # resolved once: never a partial executable path
DEFAULT_BRANCH = "main"
TIMEOUT_S = 120


class GitPublisher:
    kind = "git"

    def __init__(self, config: Mapping[str, Any]) -> None:
        remote = str(config.get("remote") or "").strip()
        workdir = str(config.get("workdir") or "").strip()
        if not remote or not workdir:
            raise PublishError("PUBLISH_DESTINATION_INVALID", "remote and workdir are required")
        self.remote = remote
        self.workdir = Path(workdir)
        self.branch = str(config.get("branch") or DEFAULT_BRANCH)
        self.subdir = str(config.get("subdir") or "documents").strip("/")
        self.author = str(config.get("author") or "Agent-Colab <colab@localhost>")
        self.fail = bool(config.get("_fail", False))  # test seam for the outage case
        if GIT is None:
            raise PublishError("PUBLISH_DESTINATION_UNAVAILABLE", "git is not installed")
        self._bin: str = GIT

    # ------------------------------------------------------------------ git plumbing
    def _git(self, args: Sequence[str], cwd: Path | None = None) -> str:
        name, _, email = self.author.partition(" <")
        try:
            proc = subprocess.run(  # noqa: S603 - resolved path, list form, no shell  # nosec B603
                [self._bin, *args],
                cwd=str(cwd or self.workdir),
                capture_output=True,
                text=True,
                timeout=TIMEOUT_S,
                check=False,
                env={
                    "PATH": "/usr/bin:/bin:/usr/local/bin",
                    "HOME": str(self.workdir),
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_AUTHOR_NAME": name,
                    "GIT_COMMITTER_NAME": name,
                    "GIT_AUTHOR_EMAIL": email.rstrip(">") or "colab@localhost",
                    "GIT_COMMITTER_EMAIL": email.rstrip(">") or "colab@localhost",
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PublishError("PUBLISH_DESTINATION_UNAVAILABLE", type(exc).__name__, True) from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:300]
            if "Authentication" in detail or "Permission denied" in detail:
                raise PublishError("PUBLISH_AUTH_FAILED", detail)
            raise PublishError("PUBLISH_DESTINATION_UNAVAILABLE", detail, True)
        return str(proc.stdout)

    def _clone(self) -> None:
        if (self.workdir / ".git").exists():
            self._git(["fetch", "origin", self.branch])
            self._git(["reset", "--hard", f"origin/{self.branch}"])
            return
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._git(["clone", "--branch", self.branch, self.remote, "."], cwd=self.workdir)

    def _commit_and_push(self, target: PublishTarget, message: str) -> str:
        for name, content in canonical_files(target).items():
            path = self.workdir / self.subdir / target.workspace_id / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self._git(["add", "-A", self.subdir])
        status = self._git(["status", "--porcelain"]).strip()
        if status:
            self._git(["commit", "-m", message])
        self._git(["push", "origin", f"HEAD:{self.branch}"])
        return self._git(["rev-parse", "HEAD"]).strip()

    def _path_in_repo(self, target: PublishTarget) -> str:
        return f"{self.subdir}/{target.workspace_id}/{target.document_id}/v{target.version}.md"

    # ------------------------------------------------------------------ contract
    def _write(self, target: PublishTarget, verb: str) -> PublishRecord:
        if self.fail:
            raise PublishError("PUBLISH_DESTINATION_UNAVAILABLE", "remote is down", True)
        self._clone()
        message = f"{verb} {target.document_id} v{target.version}"
        sha = self._commit_and_push(target, message)
        path = self._path_in_repo(target)
        return PublishRecord(f"git://{sha}/{path}", sha, {"branch": self.branch, "path": path})

    def publish(self, target: PublishTarget) -> PublishRecord:
        return self._write(target, "publish")

    def update(self, target: PublishTarget) -> PublishRecord:
        return self._write(target, "update")

    def verify(self, external_ref: str, checksum: str) -> VerifyResult:
        rest = external_ref.removeprefix("git://")
        sha, _, path = rest.partition("/")
        if not sha or not path:
            raise PublishError("PUBLISH_DESTINATION_INVALID", "unsupported external ref")
        self._clone()
        try:
            content = self._git(["show", f"{sha}:{path}"])
        except PublishError:
            return VerifyResult(False, None, "missing at destination")
        actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return VerifyResult(actual == checksum, actual, "" if actual == checksum else "mismatch")

    def archive(self, external_ref: str) -> PublishRecord:
        rest = external_ref.removeprefix("git://")
        sha, _, path = rest.partition("/")
        self._clone()
        source = self.workdir / path
        if not source.exists():
            raise PublishError("PUBLISH_NOT_FOUND", "nothing published at that path")
        archived = self.workdir / self.subdir / "_archive" / Path(path).name
        archived.parent.mkdir(parents=True, exist_ok=True)
        self._git(["mv", path, str(archived.relative_to(self.workdir))])
        self._git(["commit", "-m", f"archive {Path(path).name}"])
        self._git(["push", "origin", f"HEAD:{self.branch}"])
        new_sha = self._git(["rev-parse", "HEAD"]).strip()
        return PublishRecord(external_ref, new_sha, {"archived_from": sha})


register_publisher("git", GitPublisher)
