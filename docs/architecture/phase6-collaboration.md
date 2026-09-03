# Phase 6 — Collaboration and Documentation: module ownership

Foundation: Phase 1 documents core (`server/documents/{builder,lifecycle,store,templates}.py`:
deterministic skeleton, DRAFT→ATTEMPT_FINALIZED/FINALIZED, completion gate), artifacts core
(`server/artifacts/{service,storage,links}.py`), approvals core (`server/approvals/*`), Phase 2
cards/actions (`server/channels/{task_cards,actions,renderer}.py`), Phase 4 approvals queue and
re-auth, Phase 5 schedule Runs. Placeholder migrations `0019`–`0021` are owned by the packages below.

| Package(s) | Modules | Migration | Tests |
|---|---|---|---|
| P6-02/09 brainstorm engine, summary/decision/taskify | `server/brainstorm/{engine,turns,limits,summary,decisions,taskify}.py`, `server/application/brainstorm.py`, `server/api/v1/brainstorm.py`, MCP `brainstorm_contribute`, `/colab brainstorm` grammar handlers | `0019` | V-P6-03/04/08/26/27 |
| P6-04/05/08/10 document finalizer, redaction/provenance, recurring summaries, narrative layer | `server/documents/{finalizer,sources,redaction,provenance,narrative,citations,summaries}.py`, `server/application/documents.py` extensions | `0020` | V-P6-07/09/10/11/12/13/14/19/20/23/24/28 |
| P6-03/06/07 artifact extension, publishers, publish review | `server/artifacts/{upload,scan,quarantine}.py`, `server/documents/publishers/{base,filesystem,git,bookstack}.py`, `server/application/publishing.py`, `server/api/v1/{artifacts_upload,publishing}.py` | `0021` | V-P6-05/06/15/16/17/18/21/25 |
| P6-01 approval collaboration UX (parent) | Mattermost card buttons for LOW/MEDIUM decisions, HIGH web guidance, Schedule/Run scopes; console Documents/Publishers screens | — | V-P6-01/02/22/29 |

Rules: the command bus stays the only write path; the pre-verification draft never contains a
verdict; skeleton facts are never overwritten by narrative; canaries never reach canonical or
published documents; every provenance link resolves.
