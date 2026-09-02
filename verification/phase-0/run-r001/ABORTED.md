# Run r001 aborted by the implementer (environment)

Codex's process sandbox (bundled bubblewrap, unprivileged user namespaces) cannot start any
process on this host because AppArmor restricts unprivileged user namespaces and no root is
available. The verifier reported that every local command failed before launch and was preparing
a BLOCKED report with all 20 Tests NOT_RUN. The run was stopped before a report was written; no
Verifier Report exists for r001 (events.jsonl and stderr.log are the raw record). Revision r002
runs the verifier without the Codex process sandbox in an isolated git worktree with a separate
database; the runner records any modification of the worktree outside `verification/` after the
run (ADR-0005 addendum).
