# Documentation Service core (P1-10)

Authority: spec §14.1–14.2, §8.2; development plan §10.1, §10.2, §10.4; validation plan
V-P1-18/19/20, V-P6-07/11/19/23/24.

## Layer 1: deterministic skeleton

`server/documents/templates.py` renders the canonical Markdown of spec §14.1 with the exact
headings (Purpose and Scope; Participants and Roles; Inputs and Resources Used; Process and Key
Events; Discussion, Alternatives, Decisions and Rationale; Results and Artifacts; Verification
Method and Results; Shortcomings, Risks and Open Questions; Follow-up Work; Provenance). Heading
*keys* are stable (`purpose`, `participants`, …); localized heading text (P2-16) maps onto keys.

`server/documents/builder.py` builds the document from a **source freeze**
(`SourceFreeze(task_id, up_to_recorded_seq)`): every Event tagged with the Task up to that
`recorded_seq` (Task stream, verification-run result Events, document Events), the Task fold of
those Events, and the authority tables they reference (assignments, criteria, artifact links,
verification runs/revisions/findings, usage records, accounts for display, channel, sensitive
key status). The same freeze always yields identical bytes: no wall clock, sorted queries, no LLM.
The narrative section carries a placeholder until the layer-2 Documentation Agent (P6-10).

- Draft stage (`DRAFT_PRE_VERIFICATION`): the verification section states the method (criteria,
  assigned verifier) and `Result: PENDING (pre-verification draft)`; it never contains a verdict.
- `ATTEMPT_FINALIZED` (FAILED/BLOCKED) and `FINALIZED` (PASSED only) include the verdict of the
  run's latest revision, its tests, findings, residual risks and the earlier attempts. A PASSED
  verdict cannot be rendered as an attempt, a non-PASSED verdict cannot be rendered as final.
- Sensitive Event content is **never decrypted**; the process log marks
  `[sensitive content: encrypted, not rendered]` or, after DEK destruction,
  `[sensitive content: redacted by crypto-shredding]` (V-P1-20).
- Resources come from `usage_records`; missing data yields the standard placeholders
  `UNAVAILABLE_NO_USAGE_REPORTED`, `UNAVAILABLE_NOT_REPORTED`, `UNAVAILABLE_NO_ARTIFACTS`
  (V-P6-11). Tool names are not part of §7C usage records, so `tools` is always
  `UNAVAILABLE_NOT_REPORTED` in Phase 1.
- Provenance lists channel, source freeze, every Event/Artifact/Verification ID with citation
  markers (`[[evt:…]]`, `[[art:…]]`, `[[vr:…]]`), generator + template version, document id and
  version, and the SHA-256 of the body above that line. The manifest
  (`schemas/documents/document-manifest.v1.schema.json`) repeats the provenance ids, the
  verification meta, the resource summary, `body_sha256` and the file `sha256`.

## Canonical store

`server/documents/store.py` writes `<root>/<workspace>/<document_id>/v<version>.md` and `.json`
(root `AGENT_COLAB_DOCUMENT_ROOT`, default `/var/lib/agent-colab/documents`, injectable via
`ctx.extras["document_store"]`). Versions are write-once (`DOCUMENT_VERSION_EXISTS`), files are
read-only, path segments are validated. `storage_uri = colab-doc://<workspace>/<document_id>/v<n>`.

## Lifecycle

`server/documents/lifecycle.py` / `server/application/documents.py`:

| Command | Precondition | Result |
|---|---|---|
| `DraftDocument(task_id)` (`document.draft`) | Task exists | new version `DRAFT_PRE_VERIFICATION`, `DOCUMENT_DRAFTED` |
| `FinalizeAttempt(task_id, verification_id)` (`document.finalize`) | the run targets the Task and has a terminal result (`VERIFICATION_NOT_TERMINAL` otherwise) | FAILED/BLOCKED → `ATTEMPT_FINALIZED` + `DOCUMENT_ATTEMPT_FINALIZED`; PASSED → `FINALIZED` + `DOCUMENT_FINALIZED` |

One document per Task: `document_id = doc-<sha256("task|"+task_id)[:16]>`; versions are numbered
by the `documents` row (locked `FOR UPDATE`), and `document_versions` rows are append-only (DB
trigger `trg_document_versions_immutable`). `FinalizeAttempt` is idempotent per terminal
revision (a second call returns the existing version); both commands replay on the same
idempotency key through the `document` Event stream. Document Events carry `task_id` so they
appear in later freezes of the same Task.

Hooks for the parent to wire: `on_implementation_submitted(ctx, task_id)` after
`IMPLEMENTATION_SUBMITTED`, `on_verification_terminal(ctx, task_id, verification_id)` after a
terminal verdict. Importing `server.application.documents` registers
`finalized_document_check` on the Task domain (`register_completion_check`): completing a Task
requires a `FINALIZED` version for the latest PASSED verification, else
`COMPLETION_PREREQUISITE_MISSING` (P1-04 already raises `VERIFICATION_REQUIRED` when no PASSED
verification exists). `expected_document_id(session, task_id)` returns the id that
`CompleteTask.document_id` must reference; the completion-check contract `(state, session)` does
not see the command, so equality with `CompleteTask.document_id` is left to the Task handler
(parent wiring item).

## Ambiguities resolved

- Spec §14.1 says an "immutable attempt document" per terminal VerificationRun: implemented as a
  new version of the same Task document, one per terminal revision.
- The catalog's `DOCUMENT_ATTEMPT_FINALIZED` extension carries `result`; `DOCUMENT_FINALIZED`
  requires `verification_id` and is only ever appended for PASSED.
- Byte-reproducibility includes the manifest; `created_at` timestamps live only in the DB rows.
