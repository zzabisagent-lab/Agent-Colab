"""Build the release artifacts and write an immutable manifest (P7-01, V-P7-15).

Runs the same steps as the release workflow so a release can be produced and verified locally:
build both container images, read back their content-addressed digests, generate a CycloneDX SBOM
for the Python and the JavaScript dependency trees, and record every digest and SBOM hash in
``release/manifest.json``.

A digest identifies image content, so rebuilding the same source must reproduce it. Where a base
image or a build tool makes a build non-reproducible, the manifest records the difference and the
reason instead of pretending otherwise; ``--verify`` compares a rebuild against a recorded manifest.

The manifest is pinned and signed (V-P7-15). ``source_revision`` is read from ``git rev-parse HEAD``
at build time, and a modified tree is refused unless ``--allow-dirty`` records ``dirty: true``. The
manifest is then signed with an Ed25519 key, and the detached signature and the public key are
written to ``release/manifest.sig`` and ``release/signing-key.pub``. The private key is generated on
first use outside the repository at 0600 and is never committed, logged or printed; see
``docs/operations/release-build.md`` for custody. ``--verify`` checks the signature, the source
revision, the image digests and the SBOM hashes, and fails rather than skips when a signature or
key is absent.

    uv run python -m tools.release_build --version 8.0.0
    uv run python -m tools.release_build --verify release/manifest.json \
        --expect-commit "$(git rev-parse HEAD)"
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = ROOT / "release"
IMAGES = {
    "server": "deploy/production/Dockerfile.server",
    "web-admin": "deploy/production/Dockerfile.web-admin",
}
# Signature artifacts live beside the manifest; the private half never does. Committing a signing
# key would make every signature it ever produced worthless, so it is written outside the working
# tree and the default path is documented in docs/operations/release-build.md (V-P7-15).
SIGNATURE_PATH = RELEASE_DIR / "manifest.sig"
PUBLIC_KEY_PATH = RELEASE_DIR / "signing-key.pub"
DEFAULT_KEY_PATH = Path.home() / ".local" / "share" / "agent-colab" / "release-signing.key"
KEY_PATH_ENV = "AGENT_COLAB_RELEASE_SIGNING_KEY"


def _docker(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Docker needs a group shell on hosts where the session predates the docker group."""
    command = " ".join(["docker", *args])
    if shutil.which("sg") and _needs_sg():
        return subprocess.run(
            ["sg", "docker", "-c", command], capture_output=True, text=True, check=check
        )
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def _needs_sg() -> bool:
    probe = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    return probe.returncode != 0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_image(name: str, dockerfile: str, tag: str) -> dict[str, Any]:
    build = _docker(["build", "-f", dockerfile, "-t", tag, "."], check=False)
    if build.returncode != 0:
        return {"name": name, "tag": tag, "built": False, "reason": build.stderr[-800:]}
    inspect = _docker(["image", "inspect", tag, "--format", "{{.Id}}"], check=False)
    return {
        "name": name,
        "tag": tag,
        "built": True,
        "dockerfile": dockerfile,
        "image_id": inspect.stdout.strip(),
    }


def python_sbom(out: Path) -> dict[str, Any]:
    """CycloneDX SBOM of the locked Python tree; the build tool never becomes a runtime dep."""
    result = subprocess.run(
        [
            "uvx",
            "--from",
            "cyclonedx-bom",
            "cyclonedx-py",
            "environment",
            "--output-format",
            "json",
            "--output-file",
            str(out),
            str(ROOT / ".venv"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not out.exists():
        return {"generated": False, "reason": result.stderr[-500:] or "no output"}
    document = json.loads(out.read_text())
    return {
        "generated": True,
        "path": str(out.relative_to(ROOT)),
        "sha256": sha256_file(out),
        "components": len(document.get("components", [])),
        "format": "CycloneDX",
        "spec_version": document.get("specVersion"),
    }


def javascript_sbom(out: Path) -> dict[str, Any]:
    """The pnpm lockfile is the pinned JavaScript set; record it as a component list."""
    lock = ROOT / "web-admin" / "pnpm-lock.yaml"
    if not lock.exists():
        return {"generated": False, "reason": "pnpm-lock.yaml missing"}
    listing = subprocess.run(
        ["pnpm", "list", "--depth", "Infinity", "--json", "--prod"],
        cwd=ROOT / "web-admin",
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0:
        return {"generated": False, "reason": listing.stderr[-500:]}
    components: list[dict[str, str]] = []

    def walk(deps: dict[str, Any]) -> None:
        for pkg, meta in (deps or {}).items():
            if isinstance(meta, dict):
                components.append({"name": pkg, "version": str(meta.get("version", ""))})
                walk(meta.get("dependencies", {}))

    for entry in json.loads(listing.stdout or "[]"):
        walk(entry.get("dependencies", {}))
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "metadata": {"component": {"type": "application", "name": "agent-colab-web-admin"}},
        "components": [{"type": "library", **c} for c in components],
    }
    out.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return {
        "generated": True,
        "path": str(out.relative_to(ROOT)),
        "sha256": sha256_file(out),
        "components": len(components),
        "format": "CycloneDX",
        "spec_version": "1.5",
        "lock_sha256": sha256_file(lock),
    }


class ReleaseError(RuntimeError):
    """A release that cannot be described truthfully must not be written."""


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    return result.stdout.strip()


def source_revision(*, allow_dirty: bool = False) -> dict[str, Any]:
    """The commit this build is actually built from, and whether the tree matches it.

    The manifest used to carry whatever revision was recorded when the file was last touched, which
    is how it came to name a commit that was not the one released (V-P7-15). The revision is read
    from ``git rev-parse HEAD`` at build time instead, and a modified tree is not silently
    described by the commit it no longer matches: the build refuses unless ``--allow-dirty`` is
    given, and then it records ``dirty: true`` so the manifest says so out loud.
    """
    commit = _git("rev-parse", "HEAD")
    if not commit:
        raise ReleaseError("cannot read the source revision: git rev-parse HEAD produced nothing")
    dirty = bool(_git("status", "--porcelain"))
    if dirty and not allow_dirty:
        raise ReleaseError(
            f"the working tree has uncommitted changes, so it is not commit {commit[:12]}; "
            "commit them or pass --allow-dirty to record dirty: true in the manifest"
        )
    return {"commit": commit, "dirty": dirty}


def _assert_revision_unchanged(recorded: dict[str, Any]) -> None:
    """The build takes minutes; a checkout that moved under it invalidates the recorded commit."""
    now = _git("rev-parse", "HEAD")
    if now != recorded["commit"]:
        raise ReleaseError(
            f"HEAD moved during the build ({recorded['commit'][:12]} -> {now[:12]}); "
            "the artifacts do not correspond to a single revision"
        )


def signing_key_path(override: Path | None = None) -> Path:
    return override or Path(os.environ.get(KEY_PATH_ENV) or DEFAULT_KEY_PATH)


def load_or_create_key(path: Path) -> Ed25519PrivateKey:
    """Read the Ed25519 signing key, generating one on first use. Never returns key material.

    The key lives outside the repository at 0600 so it cannot be committed by accident, and the
    directory is created at 0700. Callers print the path and the public fingerprint, never the key.
    """
    if path.exists():
        loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(loaded, Ed25519PrivateKey):
            raise ReleaseError(f"{path} is not an Ed25519 private key")
        return loaded
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = Ed25519PrivateKey.generate()
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    return key


def canonical_bytes(manifest: dict[str, Any]) -> bytes:
    """The exact bytes that are signed: the manifest without its own signature block.

    Signing the serialization the manifest file itself uses means a verifier can re-derive the
    signed bytes from the published file alone, with no separate canonicalization rules to agree
    on. The signature block is excluded because it cannot be an input to its own value.
    """
    body = {k: v for k, v in manifest.items() if k != "signature"}
    return (json.dumps(body, indent=2, sort_keys=True) + "\n").encode()


def public_key_fingerprint(public_pem: bytes) -> str:
    return hashlib.sha256(public_pem).hexdigest()


def sign_manifest(manifest: dict[str, Any], key: Ed25519PrivateKey) -> dict[str, Any]:
    """Sign the manifest and write the detached signature and public key beside it."""
    signature = key.sign(canonical_bytes(manifest))
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    RELEASE_DIR.mkdir(exist_ok=True)
    encoded = base64.b64encode(signature).decode()
    SIGNATURE_PATH.write_text(encoded + "\n")
    PUBLIC_KEY_PATH.write_bytes(public_pem)
    # both are meant to be published alongside the manifest, unlike the private half
    SIGNATURE_PATH.chmod(0o644)
    PUBLIC_KEY_PATH.chmod(0o644)
    return {
        "algorithm": "Ed25519",
        "signature": encoded,
        "signature_path": str(SIGNATURE_PATH.relative_to(ROOT)),
        "public_key_path": str(PUBLIC_KEY_PATH.relative_to(ROOT)),
        "public_key_sha256": public_key_fingerprint(public_pem),
        "signed_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def verify_signature(manifest: dict[str, Any]) -> list[str]:
    """Check the manifest signature. An absent signature or key fails; it never skips."""
    problems: list[str] = []
    block = manifest.get("signature")
    if not isinstance(block, dict) or not block.get("signature"):
        return ["signature: the manifest records none, so the release is unsigned"]
    public_path = ROOT / str(block.get("public_key_path") or PUBLIC_KEY_PATH.relative_to(ROOT))
    if not public_path.exists():
        return [f"signature: {public_path.name} is missing, so the signature cannot be checked"]
    public_pem = public_path.read_bytes()
    if block.get("public_key_sha256") and public_key_fingerprint(public_pem) != block.get(
        "public_key_sha256"
    ):
        problems.append("signature: the public key beside the manifest is not the one that signed")
    detached = ROOT / str(block.get("signature_path") or SIGNATURE_PATH.relative_to(ROOT))
    if not detached.exists():
        problems.append(f"signature: the detached signature {detached.name} is missing")
    elif detached.read_text().strip() != str(block["signature"]).strip():
        problems.append("signature: the detached signature and the manifest disagree")
    public_key = serialization.load_pem_public_key(public_pem)
    if not isinstance(public_key, Ed25519PublicKey):
        return [*problems, "signature: the published key is not an Ed25519 public key"]
    try:
        public_key.verify(base64.b64decode(str(block["signature"])), canonical_bytes(manifest))
    except (InvalidSignature, ValueError):
        problems.append("signature: does not verify; the manifest was altered after signing")
    return problems


def build(version: str, *, allow_dirty: bool = False) -> dict[str, Any]:
    """Build every image and describe the result, pinned to the revision it was built from."""
    RELEASE_DIR.mkdir(exist_ok=True)
    revision = source_revision(allow_dirty=allow_dirty)
    images = [build_image(n, f, f"agent-colab/{n}:{version}") for n, f in IMAGES.items()]
    _assert_revision_unchanged(revision)
    return {
        "schema_id": "colab.release-manifest.v1",
        "version": version,
        "source_revision": revision["commit"],
        "dirty": revision["dirty"],
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "images": images,
        "sbom": {
            "python": python_sbom(RELEASE_DIR / f"sbom-python-{version}.json"),
            "javascript": python_js_guard(RELEASE_DIR / f"sbom-javascript-{version}.json"),
        },
    }


def python_js_guard(out: Path) -> dict[str, Any]:
    try:
        return javascript_sbom(out)
    except Exception as exc:
        return {"generated": False, "reason": f"{type(exc).__name__}: {exc}"}


#: Paths that describe a release rather than being part of it. Writing the manifest necessarily
#: creates a commit after the one it records, so a manifest can never name the commit that carries
#: it. Changes confined to these paths therefore do not break the pin; anything else does.
RELEASE_PATHS = ("release/",)


def _source_paths_changed_between(recorded: str, expected: str) -> set[str] | None:
    """Source files that differ between two commits, ignoring the release artifacts themselves.

    Returns ``None`` when the two commits cannot be compared — an unknown commit is a different
    failure from "the source moved", and is reported as its own problem rather than as drift.
    """
    if any(_git("cat-file", "-t", ref) != "commit" for ref in (recorded, expected)):
        return None
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{recorded}..{expected}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    changed = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return {path for path in changed if not path.startswith(RELEASE_PATHS)}


def verify(
    manifest_path: Path, *, expect_commit: str | None = None, require_clean: bool = False
) -> int:
    """Verify the release artifacts a manifest pins (V-P7-15).

    Three things are checked as pass/fail gates, and a fourth is reported:

    * **Authenticity.** The manifest must carry an Ed25519 signature that verifies against the
      public key published beside it. A missing signature, a missing key or a manifest edited
      after signing all fail; none of them is skipped, because a check that skips when the
      evidence is absent is not a check.
    * **Provenance.** The recorded source revision must be a real commit, and ``--expect-commit``
      asserts it is the one being released. ``dirty`` is always reported; ``--require-clean`` makes
      it a failure, which is what the published-release gate uses.
    * **Immutability.** Every recorded image digest must still resolve to exactly that content,
      and every SBOM file must still hash to its recorded value. This is what a digest is for: it
      names content, so a published digest can never quietly come to mean something else.
    * **Reproducibility.** A rebuild is attempted and the result reported. Container builds embed
      build timestamps and layer metadata, so a rebuilt image legitimately carries a different id
      even from identical sources; the manifest records that rather than claiming otherwise.
    """
    recorded = json.loads(manifest_path.read_text())
    problems: list[str] = verify_signature(recorded)
    resolved: list[dict[str, Any]] = []

    revision = str(recorded.get("source_revision") or "")
    if not revision or revision == "unknown":
        problems.append("source revision: the manifest records none")
    elif _git("cat-file", "-t", revision) != "commit":
        problems.append(f"source revision: {revision[:12]} is not a commit in this repository")
    if recorded.get("dirty") and require_clean:
        problems.append(
            "source revision: built from a modified tree (dirty: true), which --require-clean "
            "forbids; a published release must be built from a committed revision"
        )
    if expect_commit and revision != expect_commit:
        drifted = _source_paths_changed_between(revision, expect_commit)
        if drifted is None:
            problems.append(
                f"source revision: manifest records {revision[:12]}, expected "
                f"{expect_commit[:12]}, and the two cannot be compared"
            )
        elif drifted:
            listed = ", ".join(sorted(drifted)[:5])
            problems.append(
                f"source revision: manifest records {revision[:12]}, expected "
                f"{expect_commit[:12]}, and source changed between them ({listed})"
            )

    for image in recorded.get("images", []):
        image_id = str(image.get("image_id", ""))
        if not image.get("built") or not image_id:
            problems.append(f"{image['name']}: manifest records no digest")
            continue
        inspect = _docker(["image", "inspect", image_id, "--format", "{{.Id}}"], check=False)
        found = inspect.stdout.strip()
        if inspect.returncode != 0:
            problems.append(f"{image['name']}: digest {image_id[:23]}… no longer resolves")
        elif found != image_id:
            problems.append(f"{image['name']}: digest resolves to {found[:23]}…")
        resolved.append({"name": image["name"], "digest": image_id, "resolves": found == image_id})

    for language, sbom in recorded.get("sbom", {}).items():
        if not sbom.get("generated"):
            problems.append(f"sbom {language}: manifest records none")
            continue
        path = ROOT / str(sbom["path"])
        if not path.exists():
            problems.append(f"sbom {language}: {sbom['path']} is missing")
        elif sha256_file(path) != sbom["sha256"]:
            problems.append(f"sbom {language}: content changed since the manifest was written")

    rebuilt = build(str(recorded["version"]) + "-verify", allow_dirty=True)
    reproduced = []
    for old, new_image in zip(recorded.get("images", []), rebuilt["images"], strict=False):
        same = new_image.get("built") and old.get("image_id") == new_image.get("image_id")
        reproduced.append(
            {
                "name": new_image["name"],
                "reproduced": bool(same),
                "reason": None
                if same
                else "container builds embed build timestamps and layer metadata, so the "
                "rebuilt image carries a different id; the published digest is the immutable "
                "identifier of the released content",
            }
        )

    signature = recorded.get("signature") or {}
    report = {
        "verified": not problems,
        "problems": problems,
        "source_revision": revision,
        "dirty": bool(recorded.get("dirty")),
        "signature": {
            "algorithm": signature.get("algorithm"),
            "public_key_sha256": signature.get("public_key_sha256"),
            "verified": not any(p.startswith("signature:") for p in problems),
        },
        "digests_resolve": resolved,
        "reproducibility": reproduced,
    }
    print(json.dumps(report, indent=2))
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="0.0.0-dev")
    parser.add_argument("--out", type=Path, default=RELEASE_DIR / "manifest.json")
    parser.add_argument("--verify", type=Path, help="rebuild and compare with this manifest")
    parser.add_argument("--sbom-only", action="store_true", help="skip the image builds")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="build from a modified tree and record dirty: true instead of refusing",
    )
    parser.add_argument(
        "--expect-commit", help="with --verify, require the manifest to name this revision"
    )
    parser.add_argument(
        "--require-clean",
        action="store_true",
        help="with --verify, fail a manifest built from a modified tree (the release gate)",
    )
    parser.add_argument(
        "--signing-key",
        type=Path,
        help=f"Ed25519 private key; default ${KEY_PATH_ENV} or {DEFAULT_KEY_PATH}",
    )
    args = parser.parse_args(argv)

    if args.verify:
        return verify(
            args.verify, expect_commit=args.expect_commit, require_clean=args.require_clean
        )

    RELEASE_DIR.mkdir(exist_ok=True)
    try:
        if args.sbom_only:
            revision = source_revision(allow_dirty=args.allow_dirty)
            manifest = {
                "schema_id": "colab.release-manifest.v1",
                "version": args.version,
                "source_revision": revision["commit"],
                "dirty": revision["dirty"],
                "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "images": [],
                "sbom": {
                    "python": python_sbom(RELEASE_DIR / f"sbom-python-{args.version}.json"),
                    "javascript": python_js_guard(
                        RELEASE_DIR / f"sbom-javascript-{args.version}.json"
                    ),
                },
            }
        else:
            manifest = build(args.version, allow_dirty=args.allow_dirty)
        key_path = signing_key_path(args.signing_key)
        manifest["signature"] = sign_manifest(manifest, load_or_create_key(key_path))
    except ReleaseError as exc:
        print(f"release build refused: {exc}", file=sys.stderr)
        return 1
    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    # the path only; the key itself is never read back out of this process
    print(f"signed with the key at {key_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
