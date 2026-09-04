"""V-P7-15: the release artifacts carry immutable digests and a real SBOM.

The manifest a release writes must pin an image digest that still resolves to exactly that
content, and list a CycloneDX SBOM whose recorded hash still matches and whose component set is
the pinned dependency set. Rebuild reproducibility is reported rather than asserted: container
builds embed timestamps, so a rebuilt image legitimately differs while the published digest stays
the immutable identifier of what was released.

The manifest must also say truthfully what it describes: the revision it was actually built from,
whether that tree was clean, and an Ed25519 signature over its own bytes that verifies against the
public key published beside it. A missing signature or key fails verification instead of being
skipped.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools import release_build

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


@requires_manifest
def test_manifest_is_signed_and_the_signature_verifies() -> None:
    """The manifest was unsigned, so nothing tied it to whoever built it (V-P7-15)."""
    from tools import release_build

    manifest = json.loads(MANIFEST.read_text())
    signature = manifest.get("signature")
    assert signature, "the release manifest carries no signature"
    assert signature["algorithm"] == "Ed25519"
    assert signature["signature"], "the signature is empty"
    assert release_build.verify_signature(manifest) == []


@requires_manifest
def test_signature_artifacts_are_published_and_the_private_key_is_not() -> None:
    from tools import release_build

    detached = ROOT / "release" / "manifest.sig"
    public = ROOT / "release" / "signing-key.pub"
    assert detached.exists() and public.exists()
    assert public.read_bytes().startswith(b"-----BEGIN PUBLIC KEY-----")
    # a private key inside the working tree would be committed and every signature made worthless
    assert release_build.DEFAULT_KEY_PATH.is_absolute()
    assert ROOT not in release_build.DEFAULT_KEY_PATH.parents
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.split()
    candidates = [ROOT / n for n in tracked if n.endswith((".key", ".pem", ".pub"))]
    candidates += [p for p in (ROOT / "release").iterdir() if p.is_file()]
    for path in candidates:
        if b"PRIVATE KEY" in path.read_bytes():
            raise AssertionError(f"{path.relative_to(ROOT)} holds a private key")


@requires_manifest
def test_verification_fails_when_the_signature_is_absent_rather_than_skipping() -> None:
    """A check that passes when the evidence is missing is not a check."""
    from tools import release_build

    manifest = json.loads(MANIFEST.read_text())
    assert release_build.verify_signature({k: v for k, v in manifest.items() if k != "signature"})


@requires_manifest
def test_verification_fails_when_the_public_key_is_absent(tmp_path: Path) -> None:
    from tools import release_build

    manifest = json.loads(MANIFEST.read_text())
    manifest["signature"] = {**manifest["signature"], "public_key_path": str(tmp_path / "gone.pub")}
    problems = release_build.verify_signature(manifest)
    assert problems and "missing" in problems[0]


@requires_manifest
def test_verification_fails_when_the_manifest_was_altered_after_signing() -> None:
    from tools import release_build

    manifest = json.loads(MANIFEST.read_text())
    manifest["version"] = manifest["version"] + "-tampered"
    problems = release_build.verify_signature(manifest)
    assert any("does not verify" in p for p in problems), problems


@requires_manifest
def test_manifest_records_the_revision_it_was_built_from() -> None:
    """The manifest named a stale commit, so it did not describe the release (V-P7-15)."""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["source_revision"] == head, (
        "the release manifest names a different revision than the tree it is being verified "
        "against; rebuild it with: uv run python -m tools.release_build --version "
        f"{manifest['version']}"
    )
    assert "dirty" in manifest, "the manifest must say whether the tree it was built from was clean"


def test_a_dirty_tree_is_refused_unless_it_is_recorded_explicitly() -> None:
    """Recording a commit the tree no longer matches is how the manifest came to lie."""
    from tools.release_build import ReleaseError, source_revision

    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()
    )
    if not dirty:
        recorded = source_revision()
        assert recorded["dirty"] is False
        return
    with pytest.raises(ReleaseError, match="uncommitted changes"):
        source_revision()
    assert source_revision(allow_dirty=True)["dirty"] is True


def test_a_manifest_pin_survives_committing_the_manifest() -> None:
    """Writing the manifest makes a commit after the one it records; that is not drift.

    A manifest can never name the commit that carries it, so a strict equality check on the pin is
    unsatisfiable — it failed V-P7-15 for exactly that reason. Changes confined to the release
    artifacts are allowed; a change to any source file is still drift and still fails.
    """
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    parent = subprocess.run(
        ["git", "rev-parse", "HEAD~1"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()

    changed = release_build._source_paths_changed_between(parent, head)
    assert changed is not None, "two real commits must be comparable"

    unknown = release_build._source_paths_changed_between("0" * 40, head)
    assert unknown is None, "an unknown commit is reported as uncomparable, not as drift"
