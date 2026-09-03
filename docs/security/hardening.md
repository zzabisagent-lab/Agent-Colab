# Security hardening (P7-05)

## Threat controls this phase adds

| Control | Where | Verified by |
|---|---|---|
| A database outage answers 503, never 500, and readiness fails | `server/api/errors.py`, `server/observability/health.py` | V-P7-06 |
| A provider outage never loses queued deliveries | `server/channels/outbox.py::requeue_dead`, the gateway maintenance tick | V-P7-05 |
| Rotating a credential rejects the old one immediately | `server/application/accounts.py`, `server/identity/principals.py` | V-P7-12 |
| Released images ship no known-vulnerable OS package | `deploy/production/Dockerfile.server` | V-P7-11 |
| A published digest always names the same content | `tools/release_build.py --verify` | V-P7-15 |
| A release manifest is signed and names the commit it was built from | `tools/release_build.py` | V-P7-15 |

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
- **container** — Trivy at High and Critical against **every image a release ships**, which is
  both `server` and `web-admin`. Three things that used to let this scan report a clean result
  without having looked now block it outright rather than recording `SCANNER_UNAVAILABLE`: a
  manifest that pins only one of the two images, a Trivy invocation that fails, and a tag that has
  drifted away from the image id the manifest records. That last one matters most: a tag is
  mutable, so scanning `agent-colab/server:8.0.0` proves nothing unless it still resolves to the
  digest being released. Each scanned image is recorded in the report with its name, tag and
  resolved image id.
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

The `util-linux` family goes for the same reason, and it is worth recording how it got there. Four
Debian 13 advisories against `util-linux` 2.41.5 were published after the image was first scanned
clean, and no fixed version exists. They span nine binary packages — `util-linux`, `bsdutils`,
`login`, `mount`, `libmount1`, `libblkid1`, `libsmartcols1`, `libuuid1` and `liblastlog2-2` — which
is where the 36 High findings came from: four advisories times nine packages, all in the server
image, none in `web-admin`. Nothing in the build changed; the vulnerability database did. A release
image is therefore only as clean as its most recent scan, which is why the container gate reruns
against the pinned digests rather than trusting a recorded result.

The server is a Python process that never mounts a filesystem, opens a login session or reads a
partition table, so the whole family is removed. `util-linux` and `bsdutils` are marked *essential*
and `login` is *protected*, so the purge passes `--force-remove-essential` and
`--force-remove-protected`, and it runs after `useradd` has already created the service account.
The purge is verified twice over: the build fails unless every named package ends up fully
`not-installed` — a package left half-removed still appears in the dpkg database and would still be
reported — and the resulting image is confirmed to import the application and answer `/healthz` and
`/readyz` against a real database.

| Image | High before | High after |
|---|---|---|
| `agent-colab/server` | 36 | 0 |
| `agent-colab/web-admin` | 0 | 0 |

Packages that do have fixes are handled by `apt-get upgrade` in both build stages, so the image
never ships a patched-upstream vulnerability just because the base tag is a few weeks old. Moving
to a different base was measured rather than assumed: Debian 12 carries 86 High and 9 Critical and
Alpine 19 High and 2 Critical, both worse than the purged Debian 13 image.

## How a release is signed

`release/manifest.json` is signed with **Ed25519** over its own canonical bytes, which are the
manifest serialized exactly as it is written to disk with the `signature` block removed. A verifier
can therefore re-derive the signed bytes from the published file alone.

- `release/manifest.sig` — the detached signature, base64.
- `release/signing-key.pub` — the public key, PEM. Both are published with the manifest.
- The private key is **never** in the repository. It is generated on first use at
  `~/.local/share/agent-colab/release-signing.key`, mode 0600 in a 0700 directory, and is never
  logged or printed. Custody is documented in `docs/operations/release-build.md`.

`--verify` fails, rather than skips, when the signature or the public key is absent: a check that
passes because its evidence is missing is not a check. It also fails when the manifest was edited
after signing, and when the recorded `source_revision` is not a commit in the repository.

## Residual risks

`docs/security/residual-risks.md` records anything Medium or lower with an owner and a deadline.
