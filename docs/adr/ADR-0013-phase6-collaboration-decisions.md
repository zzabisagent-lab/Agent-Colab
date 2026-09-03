# ADR-0013: Phase 6 collaboration and documentation decisions

- Status: Accepted (Phase 6)
- Date: 2026-09-03

## Decisions

1. **Approval buttons are a convenience, never an authority.** An approval request posts one card
   into its channel; LOW and MEDIUM decisions may be pressed there, HIGH and CRITICAL show
   web-console guidance and refuse the press. Every callback re-checks the decision server-side,
   and self-approval is rejected and audited.
2. **A limit breach both refuses and pauses.** Development plan §7F lists consecutive same-Agent
   turns among the pause triggers while V-P6-26 calls the utterance rejected, so a breach refuses
   the contribution with its code and pauses the session with a guidance request. Because the
   refusal rolls the caller's transaction back, the pause is written in its own transaction, the
   pattern already used for policy denial audits.
3. **Only Tasks reach FINALIZED.** The `DOCUMENT_FINALIZED` Event requires a `verification_id`,
   and only a Task has a VerificationRun; Brainstorm, Run and period documents therefore publish
   their latest reviewed draft rather than weakening the Phase 1 Event contract.
4. **Provenance quotes the freeze's manifest hash**, not its id or timestamp, so the same sources
   rebuild byte-identically and a redraft returns the existing version.
5. **A scan that cannot run quarantines.** An unreachable scanner is never read as "clean";
   the artifact is quarantined with `ARTIFACT_SCAN_UNAVAILABLE` and is unreadable by the normal path.
6. **`verify` re-reads the destination** rather than trusting the recorded checksum, so a
   silently changed published document is detected; republish after an outage is idempotent per
   document, version and destination.
7. **Publish credentials never live in destination configuration**: registering a destination with
   an inline token is refused, and credentials resolve through the Secret Broker at publish time.
8. **The documented command surface must work.** The grammar advertises schedule verbs, so the
   router mounts them; a resource that is advertised but unmounted is a contract defect, not a
   phase gate.

## Consequences

- Adding a publisher is a registration plus the same contract tests.
- The narrative layer can be absent: a skeleton-only document remains valid and records why.
