# Residual security risks

Findings the release accepts, each with an owner and a deadline. High and Critical findings are
never listed here: they block the release (V-P7-11).

| Finding | Severity | Owner | Deadline | Note |
|---|---|---|---|---|
| Container rebuilds are not bit-reproducible | Informational | agent-claude-code | Phase 8 | Container builds embed timestamps and layer metadata. The published digest is the immutable identifier and `tools/release_build.py --verify` proves it still resolves; reproducibility is reported, not claimed. |
| Image signing is not performed | Low | agent-claude-code | first deployment | No registry or signing key exists in this environment. The release manifest records digests and SBOM hashes, which is what a signature would attest; signing is a deployment-time step. |
| The dynamic scan runs in-process by default | Low | agent-claude-code | first deployment | It drives the real ASGI application including middleware. A deployed instance should be scanned over the network with `--base-url`. |

As of this release the scans report **zero** High or Critical findings, so nothing else is listed.
