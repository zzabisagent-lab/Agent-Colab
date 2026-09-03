# Building a release (P7-01)

```
uv run python -m tools.release_build --version 8.0.0
uv run python -m tools.release_build --verify release/manifest.json
uv run python -m tools.security_scan --all --require sast dependency container dynamic
```

The build produces:

- **Images** from `deploy/production/Dockerfile.server` and `Dockerfile.web-admin`, each recorded
  by its content-addressed digest.
- **SBOMs** in CycloneDX form: the Python environment through `cyclonedx-py`, the JavaScript tree
  from the pnpm lockfile. Both tools run through `uvx`/`pnpm`, so neither becomes a runtime
  dependency.
- **`release/manifest.json`** pinning the version, the source revision, every image digest and
  every SBOM path with its SHA-256.

## What `--verify` proves

Immutability is the gate: each recorded digest must still resolve to that exact content, and each
SBOM file must still hash to its recorded value. Reproducibility is reported separately — a rebuilt
container image carries a different id even from identical sources because the build embeds
timestamps and layer metadata, and the manifest says so rather than claiming otherwise.

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
