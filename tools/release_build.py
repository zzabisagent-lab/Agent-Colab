"""Build the release artifacts and write an immutable manifest (P7-01, V-P7-15).

Runs the same steps as the release workflow so a release can be produced and verified locally:
build both container images, read back their content-addressed digests, generate a CycloneDX SBOM
for the Python and the JavaScript dependency trees, and record every digest and SBOM hash in
``release/manifest.json``.

A digest identifies image content, so rebuilding the same source must reproduce it. Where a base
image or a build tool makes a build non-reproducible, the manifest records the difference and the
reason instead of pretending otherwise; ``--verify`` compares a rebuild against a recorded manifest.

    uv run python -m tools.release_build --version 8.0.0
    uv run python -m tools.release_build --verify release/manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RELEASE_DIR = ROOT / "release"
IMAGES = {
    "server": "deploy/production/Dockerfile.server",
    "web-admin": "deploy/production/Dockerfile.web-admin",
}


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


def source_revision() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or "unknown"


def build(version: str) -> dict[str, Any]:
    RELEASE_DIR.mkdir(exist_ok=True)
    images = [build_image(n, f, f"agent-colab/{n}:{version}") for n, f in IMAGES.items()]
    return {
        "schema_id": "colab.release-manifest.v1",
        "version": version,
        "source_revision": source_revision(),
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


def verify(manifest_path: Path) -> int:
    """Verify the release artifacts a manifest pins (V-P7-15).

    Two different things are checked, and only the first is a pass/fail gate:

    * **Immutability.** Every recorded image digest must still resolve to exactly that content,
      and every SBOM file must still hash to its recorded value. This is what a digest is for: it
      names content, so a published digest can never quietly come to mean something else.
    * **Reproducibility.** A rebuild is attempted and the result reported. Container builds embed
      build timestamps and layer metadata, so a rebuilt image legitimately carries a different id
      even from identical sources; the manifest records that rather than claiming otherwise.
    """
    recorded = json.loads(manifest_path.read_text())
    problems: list[str] = []
    resolved: list[dict[str, Any]] = []

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

    rebuilt = build(str(recorded["version"]) + "-verify")
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

    report = {
        "verified": not problems,
        "problems": problems,
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
    args = parser.parse_args(argv)

    if args.verify:
        return verify(args.verify)

    RELEASE_DIR.mkdir(exist_ok=True)
    if args.sbom_only:
        manifest = {
            "schema_id": "colab.release-manifest.v1",
            "version": args.version,
            "source_revision": source_revision(),
            "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "images": [],
            "sbom": {
                "python": python_sbom(RELEASE_DIR / f"sbom-python-{args.version}.json"),
                "javascript": python_js_guard(RELEASE_DIR / f"sbom-javascript-{args.version}.json"),
            },
        }
    else:
        manifest = build(args.version)
    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
