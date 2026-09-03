# Building a release (P7-01)

```
uv run python -m tools.release_build --version 8.0.0
uv run python -m tools.release_build --verify release/manifest.json \
    --expect-commit "$(git rev-parse HEAD)" --require-clean
uv run python -m tools.security_scan --all --require sast dependency container dynamic
```

Build from a committed tree. The build reads `git rev-parse HEAD` at build time and refuses a tree
with uncommitted changes, because a manifest that names a commit the tree no longer matches does
not describe what was released. `--allow-dirty` proceeds and records `dirty: true` in the manifest
instead of hiding it; `--require-clean` makes a verifier reject such a manifest.

The build produces:

- **Images** from `deploy/production/Dockerfile.server` and `Dockerfile.web-admin`, each recorded
  by its content-addressed digest.
- **SBOMs** in CycloneDX form: the Python environment through `cyclonedx-py`, the JavaScript tree
  from the pnpm lockfile. Both tools run through `uvx`/`pnpm`, so neither becomes a runtime
  dependency.
- **`release/manifest.json`** pinning the version, the source revision it was actually built from,
  whether that tree was clean, every image digest and every SBOM path with its SHA-256.
- **`release/manifest.sig`** and **`release/signing-key.pub`** — the detached Ed25519 signature over
  the manifest and the public key that checks it. Both are published with the release.

## What `--verify` proves

Three gates, and one thing reported rather than gated:

- **Authenticity.** The manifest carries an Ed25519 signature over its own canonical bytes — the
  manifest as written to disk, with the `signature` block removed — which must verify against
  `release/signing-key.pub`. A missing signature, a missing key, a key that is not the one that
  signed, or a manifest edited after signing all fail. None of them is skipped.
- **Provenance.** `source_revision` must be a commit in the repository. `--expect-commit` asserts
  it is the revision being released, and `--require-clean` rejects a manifest built from a modified
  tree.
- **Immutability.** Each recorded digest must still resolve to that exact content, and each SBOM
  file must still hash to its recorded value.
- **Reproducibility** is reported, not gated — a rebuilt container image carries a different id even
  from identical sources because the build embeds timestamps and layer metadata, and the manifest
  says so rather than claiming otherwise.

## Signing key custody

The signing key is an Ed25519 private key held **outside the repository**. Committing it would make
every signature it ever produced worthless, so nothing in the build ever writes it into the working
tree, logs it, or prints it.

| | |
|---|---|
| Default path | `~/.local/share/agent-colab/release-signing.key` |
| Override | `--signing-key PATH`, or `AGENT_COLAB_RELEASE_SIGNING_KEY` |
| Permissions | `0600`, in a `0700` directory, both set by the tool |
| Format | PEM, PKCS#8, unencrypted |
| Created | on first use, if the path does not exist |

Operational rules:

1. **Back it up out of band.** Losing the key does not invalidate published signatures, but every
   later release will be signed by a different key, and consumers pinning the old public key will
   reject them.
2. **Publish the public half with each release.** `release/signing-key.pub` travels with the
   manifest, and the manifest records its SHA-256 in `signature.public_key_sha256` so a consumer can
   pin the key rather than trusting whatever key arrives beside the file.
3. **Rotate by generating a new key and announcing the new fingerprint.** Move the old key aside and
   the next build creates a fresh one; the fingerprint change is visible in the manifest.
4. **In CI, provide the key as a secret file** written to a path given by
   `AGENT_COLAB_RELEASE_SIGNING_KEY`, never as a checked-in file and never echoed into a log. The
   build prints only the path it signed with.

## In CI

`.github/workflows/ci.yml` runs the gates and the security scans on every push. 
`.github/workflows/release.yml` runs on a `v*` tag: it builds, verifies and scans, then uploads the
manifest, the SBOMs and the container scan report as release artifacts.

## The server image

The image installs the project with `uv sync --no-dev --no-editable` and copies `schemas`, `policy`
and `i18n` **inside site-packages**, because the code resolves those data roots next to the
installed package. It then removes OS packages the runtime does not use (see
`docs/security/hardening.md`) and fails the build if any survives. It runs as an unprivileged user,
declares a `HEALTHCHECK` against `/readyz`, and defaults to `--host 0.0.0.0 --port 8080` so the
container serves on its own interface while the process default stays loopback for Setup.
