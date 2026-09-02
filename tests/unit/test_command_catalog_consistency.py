"""The command grammar table (P0-10) and the risk catalog (P0-12) must agree on permissions."""

from __future__ import annotations

from server.channels.commands import VERBS
from server.policy.catalog import PolicyCatalog


def test_every_command_permission_matches_the_catalog() -> None:
    catalog = PolicyCatalog()
    actions = catalog.risk_rules["actions"]
    vocabulary = catalog.vocabulary()
    mismatches: list[str] = []
    for spec in VERBS:
        name = f"command:{spec.resource}.{spec.verb}" if spec.verb else f"command:{spec.resource}"
        entry = actions.get(name)
        if entry is None:
            mismatches.append(f"{name}: missing in policy/risk-rules.yaml")
            continue
        if entry["permission"] != spec.permission:
            mismatches.append(f"{name}: catalog {entry['permission']} != table {spec.permission}")
        if spec.permission != "none" and spec.permission not in vocabulary:
            mismatches.append(f"{name}: {spec.permission} not in vocabulary")
    assert not mismatches, "\n".join(mismatches)


def test_only_help_and_link_are_open_to_unlinked_users() -> None:
    open_resources = {s.resource for s in VERBS if s.unlinked_allowed}
    assert open_resources == {"help", "link"}
    assert {s.permission for s in VERBS if s.resource == "help"} == {"none"}
