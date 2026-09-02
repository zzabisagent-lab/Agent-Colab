# Artifact Core (P1-09)

Authority: spec §9.1 (Artifact, ArtifactLink), §9.2, §15.5/11; development plan §6.8, §7.2.

## Storage

`server/artifacts/storage.py` stores blobs content-addressed under
`<AGENT_COLAB_ARTIFACT_ROOT>/<workspace_id>/<sha256[:2]>/<sha256>` (default root
`/var/lib/agent-colab/artifacts`, the Compose `artifacts` volume). Writes stream to a temp file
while computing SHA-256, enforce the size limit (default 100 MB → `ARTIFACT_TOO_LARGE`), the
MIME/extension deny list (executables, scripts → `ARTIFACT_MIME_DENIED`), and the file-name rule
(no separators, `..`, absolute or drive-prefixed names, control characters →
`ARTIFACT_PATH_INVALID`). Blobs are stored read-only; every read re-verifies the checksum
(`ARTIFACT_CHECKSUM_MISMATCH`, `ARTIFACT_MISSING`). `storage_uri` is `colab-fs://<ws>/<sha256>`.
A `Scanner` hook (`NoopScanner` in Phase 1, ClamAV in P6-03) decides `verified` vs `quarantined`.

## Subject links

`server/artifacts/links.py` is the subject handler registry of §6.8. Subject types are fixed to
`task | schedule_run | brainstorm | decision` (DB CHECK). `task` is active in Phase 1 and checks
existence + workspace in `tasks_projection`; `schedule_run` (Phase 5), `brainstorm` and
`decision` (Phase 6) are registered as inactive stubs that return `SUBJECT_TYPE_NOT_ACTIVE` with
zero side effects. `link_artifact` validates activation, artifact existence and workspace
(`WORKSPACE_MISMATCH`), subject existence (`SUBJECT_NOT_FOUND`), relation length, and uniqueness
on `(artifact_id, subject_type, subject_id, relation)` (`ARTIFACT_LINK_DUPLICATE`).

## Commands (common bus)

| Command | Permission | Event | Tables |
|---|---|---|---|
| `RegisterArtifact` (bytes, or `storage_uri`+`sha256`+`size`) | `artifact.write` | `ARTIFACT_REGISTERED` | `artifacts` |
| `VerifyArtifact` | `artifact.write` | `ARTIFACT_VERIFIED` / `ARTIFACT_QUARANTINED` | `artifacts.status` |
| `LinkArtifact` | `artifact.write` | — (link rows are authority; see note) | `artifact_links` |
| `ArchiveArtifact` | `artifact.write` | — | `artifacts.status` |
| `read_artifact` (query) | `artifact.read` | — | — |

ACL: the creator, accounts listed in `acl.readers`, readers of every actively linked subject
(task assignee/delegator), and workspace admins may read; everyone else receives the normalized
`NOT_FOUND` (404) of §7.5. Quarantined artifacts cannot be linked; archived ones are immutable.

Note: spec §9.3 defines only `ARTIFACT_REGISTERED|VERIFIED|QUARANTINED`; link and archive
transitions are recorded in the authority tables (and audited by the caller) until an additive
`ARTIFACT_LINKED`/`ARTIFACT_ARCHIVED` extension is added to the Event catalog.
