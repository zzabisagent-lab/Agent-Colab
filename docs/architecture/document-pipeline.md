# Documentation pipeline (P6-04, P6-05, P6-08, P6-10)

`SOURCE_FREEZE → COLLECT → DRAFT_PRE_VERIFICATION → LINK_PROVENANCE → REDACT →
INDEPENDENT_VERIFY → FINALIZE_NEW_VERSION → HUMAN_REVIEW? → PUBLISH → ARCHIVE`
(development plan §10.1). Layer 1 is a deterministic template fill; layer 2 is optional prose that
may only explain what layer 1 already established (§10.4).

## Subjects and stages

| Subject | Collector | Stages | Publishable when |
|---|---|---|---|
| Task | `builder.collect_task_sources` | `DRAFT_PRE_VERIFICATION` → `ATTEMPT_FINALIZED` (FAILED/BLOCKED) or `FINALIZED` (PASSED) | `FINALIZED` |
| Brainstorm | `sources.collect_brainstorm` | draft per freeze | after publish review |
| Schedule Run | `sources.collect_schedule_run` | draft per freeze | after publish review |
| Schedule period | `sources.collect_schedule_period` | draft per freeze | after publish review |

Only a Task has a VerificationRun of its own, so only a Task document has the two-stage gate: the
pre-verification draft carries no verdict, a failed or blocked attempt is preserved as an
immutable `ATTEMPT_FINALIZED` version, and the Task completion gate needs the `FINALIZED` version
that matches the latest PASSED verification. The other subjects produce drafts; the publish review
(P6-07) is their gate. `finalizer.publishable_version(session, document_id)` returns the version a
publisher may take, and encodes that rule in one place.

## Entry points

| Trigger | Call | Result |
|---|---|---|
| `IMPLEMENTATION_SUBMITTED` | `application.documents.on_implementation_submitted` | Task draft, full pipeline |
| terminal verdict | `application.documents.on_verification_terminal` | attempt or finalized version |
| terminal Task | `_task_terminal_hook` (registered) | document if none exists, plus its Schedule Run's |
| terminal Schedule Run | `application.documents.on_schedule_run_terminal` | Run document |
| closed Brainstorm | `application.documents.on_brainstorm_closed` | Brainstorm document |
| closed period | `application.documents.on_schedule_period_closed` | period summary |

Every automatic path is failure-tolerant: a subject that cannot be documented records one stable
reason code in `document_generation_failures` and returns `None`, so one bad subject never aborts
the transition that triggered it. `finalizer.generation_report` turns that ledger into the
per-subject-type rate of V-P6-20.

## Freeze ledger

`document_freezes` stores the exact source ids a version was built from plus their hash
(`provenance.manifest_hash`, RFC 8785 canonical JSON). The rendered document quotes the *manifest
hash*, never the freeze id or its timestamp, so rebuilding the same sources produces byte-identical
output and a redraft of unchanged sources returns the existing version instead of writing a new one.

## Provenance

Every `[[evt:…]]`, `[[art:…]]`, `[[dec:…]]`, `[[vr:…]]`, `[[run:…]]` and `[[msg:…]]` reference is
recorded in `document_provenance` with the content hash the source had at freeze time.
`provenance.verify` re-resolves each one: a missing row is `MISSING`, a changed hash is
`CHECKSUM_CHANGED`, and an empty result is the V-P6-14 guarantee. A reference type whose table a
later phase will create resolves to "missing" through a `to_regclass` lookup — never through a
rollback, which would discard the caller's work.

## Redaction

`redaction.redact` runs inside the builder, before the bytes are hashed, so the canonical file, its
checksum and the manifest are clean together. Rules run most-specific first: canary, email, card
(a Luhn check keeps hex hashes from being mistaken for card numbers), phone, provider token. Only
the per-rule count and a salted sample hash are stored in `document_redactions`; the value, its
length and its plain hash never are. The source Events keep the original text — redaction is a
property of the document, not a rewrite of history.

## Narrative layer

`narrative.generate` selects an active, online Agent holding the `document.narrate` capability
(ties break by ascending `agent_id`), asks the installed `NarrativeProvider`, and lints the reply:

| Reason code | Meaning |
|---|---|
| `NARRATIVE_CITATION_MISSING` | a paragraph cites no source |
| `NARRATIVE_CITATION_UNKNOWN` | a cited id is not in the freeze |
| `NARRATIVE_CONTRADICTS_SKELETON` | a figure disagrees with the value layer 1 computed |
| `NARRATIVE_NO_AGENT_AVAILABLE` | no Documentation Agent, or none installed |
| `NARRATIVE_AGENT_DECLINED` | the Agent returned nothing or failed |

Accepted prose replaces only the *Discussion* placeholder. Rejected prose is stored with its reason
in `document_narratives` and never reaches the document; the skeleton-only draft stays valid.
Generation usage is recorded under the `document_id` scope inside a savepoint, so a missing pricing
version cannot roll back the document.

## Period summaries

`summaries.window_for` computes closed UTC windows (`daily`, `weekly`, `monthly`) and
`summaries.due_schedules` lists the Schedules whose current version asks for one. The summary
document lists every Run of the window with its status, the Tasks and Artifacts produced, and the
failures as explicit limitations.
