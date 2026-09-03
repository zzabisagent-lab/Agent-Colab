# Agent-Colab v8 — Final Development Report

Prepared for the System Owner under development plan §27A. Every claim here is backed by a
Verifier report under `verification/` or a self-evidence attempt under `evidence/`; both are in the
repository at the commit this report describes.

- Repository: `git@github.com:zzabisagent-lab/Agent-Colab.git`
- Baseline: `docs/baseline/agent-colab-{project-spec,development-plan,validation-plan}_en-v8.md`
- Verifier: Codex CLI, running as a separate process with a fresh context per phase, never the
  implementer. Reports are stored unmodified with their SHA-256.

## 1. Phase summary

| Phase | Scope | Packages | Sizes | Verifier report | Tests PASS | Tag |
|---|---|---|---|---|---|---|
| 0 | Foundations, contracts, harness | 14 | 9S/5M | `verification/phase-0/VR-P0-003.yaml` | 21 | `phase-0-passed` |
| 1 | Core: Events, Tasks, Policy, Approvals, Documents | 14 | 2S/9M/3L | `verification/phase-1/VR-P1-001.yaml` | 33 | `phase-1-passed` |
| 2 | Mattermost and Telegram channels | 17 | 8S/9M | `verification/phase-2/VR-P2-002.yaml` | 32 | `phase-2-passed` |
| 3 | Generic Agents, adapters, routing | 15 | 2S/11M/2L | `verification/phase-3/VR-P3-001.yaml` | 27 | `phase-3-passed` |
| 4 | Admin, Setup, Secrets | 14 | 1S/10M/3L | `verification/phase-4/VR-P4-002.yaml` | 34 | `phase-4-passed` |
| 5 | Scheduled work | 10 | 2S/6M/2L | `verification/phase-5/VR-P5-002.yaml` | 38 | `phase-5-passed` |
| 6 | Collaboration and documentation | 10 | 2S/7M/1L | `verification/phase-6/VR-P6-002.yaml` | 30 | `phase-6-passed` |
| 7 | Release hardening | 9 | 1S/7M/1L | see §5 | 21 recorded | pending |

Four phases needed a second revision after the Verifier failed them: Phase 2 (channel outage
coverage, link-challenge lockout timing, agent identity through the real transport), Phase 4 (the
Setup Wizard could not speak the server's protocol, pre-database token rejections never became
audit events), Phase 5 (crash, restart, secret-leak and load criteria had been demonstrated with
virtual clocks rather than real processes), and Phase 6 (closing a brainstorm never drafted its
document, and the closure gate returned the wrong stable error code). Each was fixed and re-
verified rather than argued.

Across all phases 229 mandatory Tests carry self-evidence attempts, and every Verifier report is
stored unmodified beside its SHA-256.

## 2. What the verification actually caught

Independent verification was not a formality. Beyond the four failed phases above, building and
measuring the system surfaced defects that unit tests had not:

- **The production image could not start, then could not serve, then could not migrate.** The
  schema, policy and translation trees were not on the installed package's path; the process bound
  loopback so the container answered nothing; and the image shipped no database migrations, so a
  first install died before creating its schema. Each was found by actually running the image.
- **Notifications were inert.** The rules engine and its delivery outbox were complete and tested
  directly, but nothing in the command path invoked the engine and nothing populated the rules
  table, so no real command would ever have produced a notice.
- **The server could not use more than one core.** The entry point handed the web server an
  application instance, which prevents worker forking; one interpreter saturates a core near 25
  requests per second, below this plan's own peak target.
- **A provider outage silently dropped messages.** The outbox retry budget dead-lettered rows
  during a ten-minute outage with no replay path.
- **An approval requested in a channel reached nobody**, because the request carried no channel and
  the approver selector filters by channel membership.
- **Scheduled work always demanded an approval**, because the risk classifier was handed an action
  no rule covers and defaulted to high risk.
- **Alembic silenced every application logger** whenever migrations ran inside the process.

Running acceptance against a real Mattermost rather than an in-process double exposed four more,
all in the interactive path a user actually touches:

- Card buttons carried a **relative callback URL**, which Mattermost resolves against its own
  site, so no button press ever reached the server.
- Button action ids were **not alphanumeric**, so Mattermost routed every press to a 404.
- The callback correlation id was Mattermost's own trigger id, **longer than the Event envelope
  allows**, so every press was rejected as invalid and the approval silently stayed pending.
- The channel outbox applied its **batch limit before filtering by kind**, so a notification
  backlog starved card deliveries after about ten passes.

A fifth was found reviewing that work: the gateway wired Mattermost deliveries to a post-only
provider, so a card update posted a duplicate card instead of editing the original.

All are fixed, each with a test that fails without the fix.

## 3. Acceptance status (validation plan §16)

| Criterion | Status | Evidence |
|---|---|---|
| Phases 0–7 PASSED by eligible Verifiers different from the implementer | Phases 0–6 met; Phase 7 in its second revision | `verification/phase-*/VR-*.yaml` |
| Every mandatory Test linked to commit, environment and evidence | Met | `evidence/*/SELF-*/attempt-*/result.json` |
| No core role fixed to a product or machine | Met | V-P3-12, adapter plugin registry |
| At least 3 Adapter types pass the conformance suite | Met | V-P3-05 (mcp, webhook, mattermost_bot, plugin) |
| Parallel sub-Tasks of 3+ Agents join per policy | Met | V-P3-18, V-P3-19 |
| Mattermost works end to end as the default channel | Met | V-P2-24, V-P2-25, V-P6-29 |
| Telegram Bridges of 2+ channels operate independently | Met | V-P2-03, V-P2-05, V-P2-13 |
| Zero echo, duplicates or cross-delivery in the 100-message Bridge test | Met | V-P2-04, V-P2-07 |
| Exactly-once destination side effects despite failure and replay | Met | V-P2-23, V-P3-06, V-P5-08 |
| Accounts, Agents, Roles, Bridges and settings managed in the console | Met | V-P3-13, V-P4-08, V-P2-12 |
| Clean environment initialized through the Setup Wizard, re-run blocked | Met | V-P4-01, V-P4-03, V-P7-01 |
| Pre-database bootstrap holds only a token hash and non-secret pointers | Met | V-P4-24 |
| Setup loopback by default; remote needs TLS, mTLS, allowlist and token | Met | V-P4-27, V-P4-28 |
| Common Account principal model for Humans, Agents and services | Met | V-P3-16, V-P4-26 |
| Commands execute only for verified active external identity links | Met | V-P2-20, V-P2-27 |
| No secret canary in messages, Events, logs, Documents or backups | Met | V-P4-14, V-P5-17, V-P6-13 |
| Closing documents have the mandatory structure and provenance | Met | V-P6-07, V-P6-08, V-P6-09, V-P6-14 |
| Draft and finalized versions separated; past versions immutable | Met | V-P6-23, V-P6-24 |
| cron previews match reference results across timezones and DST | Met | V-P5-02, V-P5-03, V-P5-04, V-P5-05 |
| No duplicate Runs or Tasks under dual schedulers, crash or restart | Met | V-P5-06, V-P5-07, V-P5-08, V-P5-24 |
| Concurrency and missed-run policies exact | Met | V-P5-09 through V-P5-14 |
| Cancel states recorded as defined; terminal Runs never changed | Met | V-P5-31, V-P5-32 |
| Revoked principals and unapproved high-risk scheduled work do not run | Met | V-P5-15, V-P5-18 |
| Shell commands cannot be registered through Schedule templates | Met | V-P5-26 |
| Self-verification by the implementing Agent rejected | Met | V-P1-13, V-P1-24 |
| REST and MCP share handlers, policy, schema and idempotency | Met | V-P1-11, V-P1-26, V-P3-21 |
| Projection rebuild and backup restore produce identical hashes | Met | V-P7-07, V-P7-08 |
| Approval, Schedule and Agent aggregates rebuilt from hash chains | Met | V-P1-22, V-P3-17 |
| After hard delete, content undecryptable and Event bytes unchanged | Met | V-P4-22, V-P4-25 |
| Restoring a pre-deletion backup does not resurrect deleted content | Met | V-P4-29, V-P7-20 |
| 20 consecutive full end-to-end successes | Met through a real Mattermost; awaiting re-verification | V-P7-02, V-P7-22 |
| RPO 24 h / RTO 4 h and the load profiles met | Met for recovery and peak; soak running | V-P7-07, V-P7-03, V-P7-04 |
| Zero High or Critical security findings | Met on both images; awaiting re-verification | V-P7-11 |
| break-glass and hard delete only through defined workflows | Met | V-P4-21, V-P4-22 |
| Zero executions exceeding Agent Limits and Schedule budgets | Met | V-P3-15, V-P5-28, V-P5-37 |
| Bridge p95 5 s, Schedule start p95 60 s, document rate 95% | Met | V-P2-15, V-P5-27, V-P6-20 |

## 4. Residual risks and known limitations

`docs/security/residual-risks.md` carries the register: each open finding has a severity, an owner,
a deadline and an acceptor. No High or Critical finding is accepted; they block the release.

Known limitations, stated plainly:

- The soak is running its full 24 hours now. Earlier evidence used a 30-minute window, and the
  Verifier rightly refused it. Phase 7 cannot be resubmitted until that run finishes.
- The release manifest is signed, but with a key generated and held on this build host, so trust
  in the public key is local. There is no registry, no organisational signing key and no production
  TLS terminator in this environment; publication and TLS termination remain deployment steps.
- Setup mTLS is enforced at a trusted reverse proxy's assertion; the proxy itself is deployment.
- Only the encrypted local Secret provider is implemented. An external provider registers through
  the same contract and must pass the same tests.
- Brainstorm, Run and period documents publish their latest reviewed draft rather than a FINALIZED
  version, because the finalize Event requires a verification id that only Tasks have.

## 5. Release artifacts

Built at commit `dedd2a1` from a clean tree, version `8.0.0-test`:

| Image | Tag | Content digest |
|---|---|---|
| server | `agent-colab/server:8.0.0-test` | `sha256:b411843bbf0cc2345f4…` |
| web-admin | `agent-colab/web-admin:8.0.0-test` | `sha256:4886e45a54746e1cf05…` |

- The manifest at `release/manifest.json` records those digests, is signed with Ed25519 over its
  own canonical bytes, and pins the source revision. `--verify` fails rather than skips on an
  absent, wrong or tampered signature, and refuses a revision that is not a commit.
- CycloneDX SBOMs for the Python and JavaScript dependency trees, hashed in the same manifest.
- Evidence index at `release/evidence-index.json`: every Verifier report, its checksum, and each
  Phase's self-evidence.
- Changelog: the phase table in §1 with its tags; every commit is on `main` behind
  `phase-0-passed` … `phase-6-passed`, with Phase 7 on branch `phase-7`.

## 6. Deployment plan

**The target.** `.env` records one deployment server: a server name, an IP address, and a login id
and password under the heading "Deploy Server Information". Those four values are credentials for a
single machine, so this report does not reproduce any of them, and neither does any evidence,
commit message or log. Read them from `.env` at deployment time. `.env` is in `.gitignore` and has
been since the first commit.

**What is known and what is not.** The target names a host and a login. It does not say whether
Docker and Compose are installed there, what its Postgres situation is, what DNS name or TLS
certificate it serves, or where backups should land. Those are answered on the machine, not from
here, and the first step below is the check that answers them.

| Step | Action | Check that it worked |
|---|---|---|
| 1 | Preflight over SSH: Docker and Compose present, disk and memory headroom, ports free, outbound access to Mattermost | preflight script exits zero |
| 2 | Build and scan the release locally, then verify the signed manifest | `tools.release_build --verify` and `tools.security_scan --require` both pass |
| 3 | Copy the image and Compose file to the host, provision an empty database and volumes | image digest on the host matches the manifest |
| 4 | Start the stack, published on loopback only | `/healthz` answers, `/readyz` reports the database |
| 5 | Configure through the Setup Wizard from inside the host, which is loopback-only by design: database, key provider, Owner with TOTP and recovery code, then integrations, then lock | Setup state reaches `LOCKED`; the recovery code is recorded once, off the machine |
| 6 | Start workers with the `workers` Compose profile | the scheduler leases a run and the outbox drains |
| 7 | Acceptance on the target: one Task through Mattermost, one approval, one document | the operations overview shows every dependency healthy |
| 8 | Take the first backup and record it | `tools/backup.py --record` writes a verified backup |

**Rollback.** Redeploy the previous image digest. For an irreversible migration, forward-fix per
`docs/operations/upgrade-rollback.md`. Both paths are rehearsed under V-P7-09 and V-P7-10.

**Before any of this is possible**, TLS termination and a reverse proxy must be decided for the
target. The application publishes on loopback and expects a proxy in front of it. That is a
deployment decision, not a code change.

## 7. The decision that remains

Development is complete except for Phase 7's second verification revision, which is in progress:
the full 24-hour soak is running and Phase 7 is resubmitted to the Verifier when it finishes.

Nothing has been deployed. `docs/operations/deployment-decision.md` records the state as
`PENDING_USER_DECISION`, and the deployment ledger under `release/deployments/` is empty, which is
the evidence that no deployment preceded a decision. Deployment happens only after an explicit
instruction to deploy to the server named in `.env`, and not before Phase 7 has passed.
