"""V-P7-15: the release artifacts carry immutable digests and a real SBOM.

The manifest a release writes must pin an image digest that still resolves to exactly that
content, and list a CycloneDX SBOM whose recorded hash still matches and whose component set is
the pinned dependency set. Rebuild reproducibility is reported rather than asserted: container
builds embed timestamps, so a rebuilt image legitimately differs while the published digest stays
the immutable identifier of what was released.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "release" / "manifest.json"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    if probe.returncode == 0:
        return True
    if shutil.which("sg") is None:
        return False
    return (
        subprocess.run(
            ["sg", "docker", "-c", "docker info"], capture_output=True, text=True, check=False
        ).returncode
        == 0
    )


requires_manifest = pytest.mark.skipif(
    not MANIFEST.exists(), reason="no release manifest; run tools.release_build first"
)


@requires_manifest
def test_manifest_pins_image_digests_and_sboms() -> None:
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["schema_id"] == "colab.release-manifest.v1"
    assert manifest["source_revision"] and manifest["built_at"]

    images = manifest["images"]
    assert {i["name"] for i in images} == {"server", "web-admin"}
    for image in images:
        assert image["built"], image.get("reason")
        # a digest is content-addressed: sha256 plus 64 hex characters, not a mutable tag
        assert image["image_id"].startswith("sha256:") and len(image["image_id"]) == 71

    for language in ("python", "javascript"):
        sbom = manifest["sbom"][language]
        assert sbom["generated"], sbom.get("reason")
        assert sbom["format"] == "CycloneDX" and sbom["spec_version"]
        assert sbom["components"] > 0
        path = ROOT / sbom["path"]
        assert path.exists(), sbom["path"]
        document = json.loads(path.read_text())
        assert len(document["components"]) == sbom["components"]


@requires_manifest
def test_recorded_sbom_hashes_still_match_their_files() -> None:
    """The SBOM a release published cannot change without the manifest changing with it."""
    import hashlib

    manifest = json.loads(MANIFEST.read_text())
    for language in ("python", "javascript"):
        sbom = manifest["sbom"][language]
        path = ROOT / sbom["path"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == sbom["sha256"], f"{language} SBOM changed since the manifest was written"


@requires_manifest
def test_python_sbom_lists_the_pinned_dependency_set() -> None:
    manifest = json.loads(MANIFEST.read_text())
    document = json.loads((ROOT / manifest["sbom"]["python"]["path"]).read_text())
    names = {c["name"].lower() for c in document["components"]}
    # the runtime dependencies the project declares must all appear in its bill of materials
    for required in ("fastapi", "sqlalchemy", "psycopg", "alembic", "pydantic"):
        assert required in names, required
    assert all(c.get("version") for c in document["components"]), "an unpinned component"


@requires_manifest
@pytest.mark.skipif(not _docker_available(), reason="docker is not available")
def test_recorded_digests_still_resolve_to_the_same_content() -> None:
    from tools.release_build import _docker

    manifest = json.loads(MANIFEST.read_text())
    for image in manifest["images"]:
        inspect = _docker(
            ["image", "inspect", image["image_id"], "--format", "{{.Id}}"], check=False
        )
        assert inspect.returncode == 0, f"{image['name']}: digest no longer resolves"
        assert inspect.stdout.strip() == image["image_id"]
