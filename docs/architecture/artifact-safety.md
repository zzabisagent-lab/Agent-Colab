# Artifact safety: upload, scanning, quarantine and links (P6-03)

Phase 1 gave Artifacts content-addressed storage, a hash chain and subject ACLs. Phase 6 adds the
admission path an untrusted upload has to survive, and activates the last two subject types.

## Admission checks, in order

| # | Check | Where | Failure |
|---|---|---|---|
| 1 | File name normalises: no traversal, separators, control characters or denied extension | `storage.validate_filename` | `ARTIFACT_PATH_INVALID` / `ARTIFACT_MIME_DENIED`, nothing stored |
| 2 | Declared MIME allowed by policy | `storage.validate_mime` | `ARTIFACT_MIME_DENIED`, nothing stored |
| 3 | Size stays under the limit while streaming | `storage.write` | `ARTIFACT_TOO_LARGE`, temp file removed |
| 4 | Declared MIME agrees with the sniffed content | `upload.mime_matches` | `ARTIFACT_MIME_MISMATCH`, nothing registered |
| 5 | Stored bytes re-hash to the returned SHA-256 | `upload.readback_hash` | quarantined `ARTIFACT_CHECKSUM_MISMATCH` |
| 6 | Malware scan is clean | `scan.report_for` | quarantined `ARTIFACT_MALWARE` |

Checks 1-4 happen before the Artifact row exists, so a refused upload leaves no trace beyond the
audit entry. Checks 5-6 quarantine: the row and its provenance survive for investigation, but the
artifact is unreadable through `GET /api/v1/artifacts/{id}/content` and cannot be linked.

## Content sniffing

`upload.sniff` reads the first 4 KiB and matches magic bytes (PNG, JPEG, GIF, PDF, ZIP, gzip, ELF,
PE, shebang, Java class, MP4), falling back to `text/plain` for printable bytes and
`application/octet-stream` otherwise. `mime_matches` then decides whether the declared type is a
truthful description: ZIP containers may be declared as any Office/OpenDocument/EPUB type, text may
be declared as any `text/*` or textual `application/*`, unknown binary may be anything except a
text claim, and executables never pass whatever they claim.

## Scanning

`Scanner` is the seam Phase 1 declared. `ClamdScanner` speaks the ClamAV `INSTREAM` protocol over
the Unix socket in `AGENT_COLAB_CLAMAV_SOCKET`; `SignatureScanner` is the pure-Python fallback that
matches known-bad markers (EICAR among them) and is what runs in environments without ClamAV. A
scan that cannot complete is `verdict="error"` and quarantines with `ARTIFACT_SCAN_UNAVAILABLE`:
an unreachable scanner never means "clean". Signature matching keeps a rolling overlap so a marker
split across two reads is still found.

Every scan is recorded in `artifact_scan_results` (scanner, verdict, reason code, signature name).
Quarantine writes `artifact_quarantine` plus one audit entry carrying the reason code and the
signature *name* — never the file's bytes, and never enough of them to reconstruct the sample.

## Releasing

An administrator with `artifact.write` releases a quarantined artifact with a reason
(`POST /api/v1/artifacts/{id}/quarantine/release`). The release is audited, the ledger row keeps
who released it and why, and a second release is a no-op rather than a second audit entry.

## Subject links (V-P6-25)

`task` activated in Phase 1 and `schedule_run` in Phase 5. Phase 6 activates the last two:

| Subject | Existence | ACL readers |
|---|---|---|
| `brainstorm` | `brainstorms.brainstorm_id` | facilitator + every row in `brainstorm_participants` |
| `decision` | `brainstorm_decisions.decision_id` | `decided_by` + the readers of its Brainstorm |

Those tables belong to the Brainstorm package (migration 0019). The handlers check the table exists
before querying it, so a link attempt during a partial rollout reports `SUBJECT_NOT_FOUND` instead
of a database error. Unknown subject types give `SUBJECT_TYPE_UNKNOWN`, unknown ids give
`SUBJECT_NOT_FOUND`, and an id from another workspace gives `WORKSPACE_MISMATCH` — each with zero
side effects.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/artifacts/upload` | multipart (`file`, `mime`, optional `subject_type`/`subject_id`/`relation`); `Idempotency-Key` required |
| GET | `/api/v1/artifacts/{id}` | metadata, quarantine state and scan history |
| GET | `/api/v1/artifacts/{id}/content` | ACL enforced, checksum re-verified, quarantine refuses with 409 |
| POST | `/api/v1/artifacts/{id}/quarantine/release` | `artifact.write`, reason required |
