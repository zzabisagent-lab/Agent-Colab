"""Permission/risk catalog linter (V-P0-18, P0-12).

Checks: every policy YAML validates against its schema; zero out-of-vocabulary permissions in
default roles, capabilities, risk-rule actions and the policy test fixture roles; zero
unclassified actions across the union of §7.4 MCP tools, §7A.2 commands (parsed from the
development plan) and the risk-rules action map; quorum/approval defaults equal §7E and
``server.domain.defaults.APPROVAL_QUORUM``; §6.9 class → risk/approval table reproduced exactly.
"""

from __future__ import annotations

import re
import sys

import yaml

from server.domain import defaults
from server.policy.catalog import PolicyCatalog, PolicyCatalogError, permission_in_vocabulary
from tools.baseline import DEV_PLAN, ROOT, parse_tables

FIXTURE_ROLES = ROOT / "tests" / "fixtures" / "policy" / "matrix.yaml"
SECTION_6_9 = {
    "read_query": ("LOW", "none"),
    "internal_write": ("LOW", "none"),
    "delegation_routing": ("MEDIUM", "channel_policy"),
    "external_send": ("HIGH", "human_1"),
    "destructive": ("HIGH", "human_1"),
    "secret_exposure": ("CRITICAL", "human_2"),
    "production_change": ("HIGH", "human_1"),
}
SECTION_6_9_MINIMUM = {
    "task.create",
    "task.read",
    "task.delegate",
    "task.accept",
    "task.progress",
    "task.submit",
    "task.complete",
    "task.cancel",
    "approval.request",
    "approval.decide",
    "approval.revoke",
    "verification.assign",
    "verification.submit",
    "artifact.write",
    "artifact.read",
    "document.draft",
    "document.finalize",
    "document.publish",
    "secret.grant",
    "secret.lease",
    "schedule.manage",
    "schedule.run",
    "agent.manage",
    "channel.manage",
    "bridge.manage",
    "brainstorm.open",
    "brainstorm.contribute",
    "brainstorm.facilitate",
    "brainstorm.summarize",
    "admin.settings",
    "admin.accounts",
    "admin.break_glass",
    "admin.hard_delete",
}
SECTION_7E_QUORUM = {"LOW": 0, "MEDIUM": 1, "HIGH": 1, "CRITICAL": 2}


def baseline_tools() -> set[str]:
    """MCP tool names from development plan §7.4 (backtick identifiers without dots)."""
    text = DEV_PLAN.read_text(encoding="utf-8")
    section = text[text.index("### 7.4 MCP Tool Surface") : text.index("### 7.5 ")]
    return {f"tool:{t}" for t in re.findall(r"`([a-z][a-z0-9_]*)`", section)}


def baseline_commands() -> set[str]:
    """resource.verb pairs from the development plan §7A.2 command grammar table."""
    out: set[str] = set()
    for table in parse_tables(DEV_PLAN.read_text(encoding="utf-8")):
        if table.header[:2] == ["resource", "verb"]:
            for row in table.rows:
                resource = row[0].strip()
                verbs = [v.strip() for v in row[1].split(",") if v.strip() and v.strip() != "—"]
                if not verbs:
                    out.add(f"command:{resource}")
                for verb in verbs:
                    out.add(f"command:{resource}.{verb}")
    return out


def lint() -> tuple[list[str], dict[str, int]]:
    problems: list[str] = []
    try:
        catalog = PolicyCatalog()
    except PolicyCatalogError as exc:
        return [f"{exc.code}: {exc.detail}"], {}
    vocab = catalog.vocabulary()
    problems.extend(
        f"§6.9 minimum permission missing: {p}" for p in sorted(SECTION_6_9_MINIMUM - vocab)
    )
    problems.extend(
        f"§6.9 minimum permission not marked minimum: {p}"
        for p in sorted(SECTION_6_9_MINIMUM - catalog.minimum_vocabulary())
    )
    for cls, (risk, approval) in SECTION_6_9.items():
        spec = catalog.risk_rules["action_classes"].get(cls)
        if spec is None or (spec["risk"], spec["approval"]) != (risk, approval):
            problems.append(f"§6.9 class {cls} must be {risk}/{approval}, got {spec}")
    quorum = catalog.risk_rules["approval_defaults"]["quorum"]
    if quorum != SECTION_7E_QUORUM:
        problems.append(f"§7E quorum mismatch: {quorum}")
    if quorum != defaults.APPROVAL_QUORUM:
        problems.append(f"quorum differs from server.domain.defaults.APPROVAL_QUORUM: {quorum}")
    ad = catalog.risk_rules["approval_defaults"]
    if (
        ad["expiry_hours"] != defaults.APPROVAL_EXPIRY_HOURS
        or ad["reminder_ratio"] != defaults.APPROVAL_REMINDER_RATIO
    ):
        problems.append("approval expiry/reminder differ from §21.1 defaults")
    if ad["human_only_from"] != "HIGH":
        problems.append("§7E: HIGH and above must be Human-only")
    actions = catalog.actions()
    expected = baseline_tools() | baseline_commands()
    unclassified = sorted(a for a in expected if a not in actions)
    problems.extend(f"unclassified action: {a}" for a in unclassified)
    for action in actions:
        if catalog.risk_for(action).unclassified:  # pragma: no cover - loader guarantees classes
            problems.append(f"unclassified action: {action}")
    role_count = 0
    for role_id, role in catalog.roles_raw["roles"].items():
        role_count += 1
        for group in ("permissions", "deny"):
            for pattern in role[group]:
                if not permission_in_vocabulary(pattern, vocab):
                    problems.append(f"role {role_id}.{group}: out-of-vocabulary {pattern}")
    fixture = yaml.safe_load(FIXTURE_ROLES.read_text(encoding="utf-8"))
    for role_id, role in fixture["roles"].items():
        for pattern in list(role.get("permissions", [])) + list(role.get("deny", [])):
            if pattern != "*" and not permission_in_vocabulary(pattern, vocab):
                problems.append(f"fixture role {role_id}: out-of-vocabulary {pattern}")
    counts = {
        "permissions": len(vocab),
        "minimum_permissions": len(catalog.minimum_vocabulary()),
        "action_classes": len(catalog.risk_rules["action_classes"]),
        "actions": len(actions),
        "baseline_tools": len(baseline_tools()),
        "baseline_commands": len(baseline_commands()),
        "unclassified": len(unclassified),
        "default_roles": role_count,
        "fixture_roles": len(fixture["roles"]),
        "capabilities": len(catalog.capabilities["capabilities"]),
    }
    return problems, counts


def main() -> int:
    problems, counts = lint()
    for p in problems:
        print(f"POLICY: {p}")
    summary = ", ".join(f"{k}={v}" for k, v in counts.items())
    print(f"policy_lint: {summary}, problems={len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
