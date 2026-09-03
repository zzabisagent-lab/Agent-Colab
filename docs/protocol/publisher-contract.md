# Publisher contract (P6-06/P6-07; development plan §10.3)

A publisher moves a FINALIZED document version — canonical Markdown plus its JSON manifest — to a
destination, and can afterwards prove that what is there still matches the canonical checksum.

```text
publish(target)                  -> PublishRecord(external_ref, external_version, detail)
update(target)                   -> PublishRecord            # a correction or a later version
verify(external_ref, checksum)   -> VerifyResult(ok, checksum, detail)
archive(external_ref)            -> PublishRecord
```

`PublishTarget` carries `workspace_id`, `document_id`, `version`, `markdown`, `manifest`,
`checksum` (SHA-256 of the canonical Markdown) and a display `title`. Publishers are registered by
kind (`register_publisher`), so a new destination type is a registration rather than a change to
the publishing command.

## Built-in kinds

| Kind | External ref | Notes |
|---|---|---|
| `filesystem` | `colab-file://<ws>/<doc>/v<n>` | temp-file rename so a reader never sees a partial write; archive moves the pair under `_archive/` |
| `git` | `git://<commit sha>/<path>` | commits into a working clone and pushes; `verify` reads the file back out of the committed tree; Gitea or any Git remote works |
| `bookstack` | `bookstack://<base>/api/pages/<id>` | reference wiki connector; one page per document, updated in place, version carried as a tag |

A Wiki.js connector would implement the same protocol; only the class changes.

## Stable error codes

`PUBLISH_DESTINATION_UNAVAILABLE` (retryable), `PUBLISH_DESTINATION_INVALID`,
`PUBLISH_AUTH_FAILED`, `PUBLISH_CHECKSUM_MISMATCH`, `PUBLISH_NOT_FOUND`, `PUBLISH_REJECTED`.

## Gates before anything is published

1. the caller holds `document.publish`;
2. the version's status is `FINALIZED` — a pre-verification draft is never published;
3. an **approved** publish review exists for exactly that version (`publish_reviews`).

A rejected review, a missing review, or a review of a different version all give
`PUBLISH_REVIEW_REQUIRED`. An unauthorized Agent is refused by the Policy Engine before the
destination is contacted.

## Exactly once, and outages

`published_documents` is unique on `(document_id, version, destination_id)`. Every try — success or
failure — appends a row to `publish_attempts` with its attempt number and error code. While a
destination is down the command fails with the retryable code, the canonical document is untouched
and nothing is recorded as published; after recovery the first successful attempt publishes, and
any further call returns the existing publication with `already_published` rather than writing a
second time.

## Corrections

A factual correction is a *new document version* published with `correction_of_version` and
`correction_reason`. The original published row stays exactly as it was, so both the corrected and
the superseded version remain visible with their own checksums.

## Credentials

Destination configuration in `publish_destinations.config` never holds a secret value: a
credential is a Secret Broker reference in `credential_ref`, resolved at publish time through the
`publish_credential_resolver` extra and passed to the publisher as `token`. Registering a
destination whose config carries a `token`/`password`/`secret`/`api_key` string is refused with
`PUBLISH_DESTINATION_SECRET_VALUE`.

## Endpoints

| Method | Path |
|---|---|
| POST | `/api/v1/publishing/destinations` |
| GET | `/api/v1/publishing/destinations` |
| POST | `/api/v1/publishing/reviews` |
| GET | `/api/v1/publishing/documents/{document_id}/reviews` |
| POST | `/api/v1/publishing` |
| POST | `/api/v1/publishing/verify` |
| POST | `/api/v1/publishing/archive` |
| GET | `/api/v1/publishing/documents/{document_id}` |
| GET | `/api/v1/publishing/documents/{document_id}/versions/{version}/attempts` |
