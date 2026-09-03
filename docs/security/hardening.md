# Security hardening (P7-05)

## Threat controls this phase adds

| Control | Where | Verified by |
|---|---|---|
| A database outage answers 503, never 500, and readiness fails | `server/api/errors.py`, `server/observability/health.py` | V-P7-06 |
| A provider outage never loses queued deliveries | `server/channels/outbox.py::requeue_dead`, the gateway maintenance tick | V-P7-05 |
| Rotating a credential rejects the old one immediately | `server/application/accounts.py`, `server/identity/principals.py` | V-P7-12 |
| Released images ship no known-vulnerable OS package | `deploy/production/Dockerfile.server` | V-P7-11 |
| A published digest always names the same content | `tools/release_build.py --verify` | V-P7-15 |

## The scans

`uv run python -m tools.security_scan --all --require sast dependency container dynamic`

Each scan writes `evidence/phase-7/scans/<name>.json`. The gate is **zero High or Critical**. A
scanner that cannot run records `SCANNER_UNAVAILABLE` with the reason; `--require` makes an
unavailable scanner a failure rather than a silent pass.

- **sast** — bandit over `server` and `sidecar` at every severity, using the repository's own
  configuration so the documented per-line suppressions still apply.
- **dependency** — pip-audit (OSV) over the installed project environment, and `pnpm audit` over
  the locked production JavaScript tree. An advisory without a rated severity is recorded as
  `UNKNOWN`, never downgraded to Low.
- **container** — Trivy against every image the release manifest pins, at High and Critical.
- **dynamic** — checks a running instance: security headers, unauthenticated access to admin
  routes, error bodies that leak a connection string or a traceback, acceptance of an unknown
  service token, and cookie flags. `tests/integration/test_security_scans.py` includes a negative
  control: pointed at an application without the middleware, these checks must fire, so a clean
  result means the checks ran rather than that they cannot detect anything.

## Why the server image purges OS packages

The Debian base carries `perl-base`, `ncurses*`, `gzip`, `libsystemd0`, `libudev1`, `libacl1` and
`libsqlite3-0`. Their outstanding advisories (3 Critical, 15 High at the time of writing) have **no
fix available** from the distribution, and none of them is used by a Python API server. Rather than
accept the risk — which an autonomous run may not do — the image removes them and the build fails
if any survives. The image is verified to import the application and serve `/healthz` afterwards.

Packages that do have fixes are handled by `apt-get upgrade` in both build stages, so the image
never ships a patched-upstream vulnerability just because the base tag is a few weeks old.

## Residual risks

`docs/security/residual-risks.md` records anything Medium or lower with an owner and a deadline.
