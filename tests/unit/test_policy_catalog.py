"""Permission/risk catalog (P0-12, V-P0-18): loader, lint, and negative cases."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from server.domain import defaults
from server.policy.catalog import (
    POLICY_DIR,
    SCHEMA_DIR,
    PolicyCatalog,
    PolicyCatalogError,
    default_catalog,
    permission_in_vocabulary,
)
from server.policy.model import ActionRequest
from tools import policy_lint


def test_real_catalog_loads_and_lint_passes() -> None:
    problems, counts = policy_lint.lint()
    assert problems == []
    assert counts["unclassified"] == 0
    assert counts["permissions"] >= 33 and counts["minimum_permissions"] == 33
    assert counts["baseline_tools"] == 29 and counts["baseline_commands"] == 45
    assert policy_lint.main() == 0


def test_section_6_9_risk_table_and_7e_quorum() -> None:
    cat = default_catalog()
    assert cat.risk_for("tool:task_get").risk == "LOW"
    assert cat.risk_for("tool:task_delegate").risk == "MEDIUM"
    assert cat.risk_for("api:llm_exposure").risk == "CRITICAL"
    assert cat.risk_for("api:settings_apply").approval == "human_1"
    assert cat.risk_for("command:doc.publish").risk == "HIGH"
    for risk, q in {"LOW": 0, "MEDIUM": 1, "HIGH": 1, "CRITICAL": 2}.items():
        assert cat.quorum(risk) == q == defaults.APPROVAL_QUORUM[risk]
    assert cat.human_only("HIGH") and cat.human_only("CRITICAL")
    assert not cat.human_only("MEDIUM")


def test_unclassified_action_falls_back_to_high_and_is_flagged() -> None:
    d = default_catalog().risk_for("api:something_new")
    assert d.unclassified is True and d.risk == "HIGH" and d.approval == "human_1"


def test_side_effect_conflict_raises_risk() -> None:
    d = default_catalog().risk_for("tool:task_progress", side_effect=True)
    assert d.risk == "MEDIUM" and d.approval == "channel_policy"
    assert default_catalog().risk_for("api:agent_revoke", side_effect=True).risk == "HIGH"


def test_default_roles_evaluate_through_vocabulary_bound_engine() -> None:
    cat = default_catalog()
    engine = cat.engine()
    worker = cat.role("role-agent-worker")
    verifier = cat.role("role-agent-verifier")
    scheduler = cat.role("role-scheduler-service")
    assert engine.evaluate([worker], ActionRequest("task.progress")).allowed
    assert engine.evaluate([worker], ActionRequest("approval.decide")).reason == "EXPLICIT_DENY"
    assert engine.evaluate([verifier], ActionRequest("task.submit")).reason == "EXPLICIT_DENY"
    assert engine.evaluate([verifier], ActionRequest("verification.submit")).allowed
    assert engine.evaluate([scheduler], ActionRequest("task.create")).reason == "EXPLICIT_DENY"
    assert engine.evaluate([scheduler], ActionRequest("schedule.run")).allowed
    assert (
        engine.evaluate([worker], ActionRequest("task.frobnicate")).reason == "UNKNOWN_PERMISSION"
    )
    assert (
        engine.evaluate([cat.role("role-administrator")], ActionRequest("admin.break_glass")).reason
        == "EXPLICIT_DENY"
    )
    assert engine.evaluate(
        [cat.role("role-system-owner")], ActionRequest("admin.break_glass")
    ).allowed


def test_wildcard_vocabulary_matching() -> None:
    vocab = frozenset({"task.read", "task.create"})
    assert permission_in_vocabulary("task.*", vocab)
    assert permission_in_vocabulary("task.read", vocab)
    assert not permission_in_vocabulary("secret.*", vocab)
    assert not permission_in_vocabulary("task.delete", vocab)


def _copy_policy(tmp_path: Path) -> Path:
    dst = tmp_path / "policy"
    shutil.copytree(POLICY_DIR, dst)
    return dst


def _edit(path: Path, mutate: object) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)  # type: ignore[operator]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_unknown_permission_in_role_is_rejected(tmp_path: Path) -> None:
    pol = _copy_policy(tmp_path)
    _edit(
        pol / "default-roles.yaml",
        lambda d: d["roles"]["role-operator"]["permissions"].append("task.delete"),
    )
    with pytest.raises(PolicyCatalogError) as exc:
        PolicyCatalog(pol, SCHEMA_DIR)
    assert exc.value.code == "POLICY_PERMISSION_UNKNOWN"


def test_action_with_unknown_class_is_rejected(tmp_path: Path) -> None:
    pol = _copy_policy(tmp_path)
    _edit(
        pol / "risk-rules.yaml",
        lambda d: d["actions"].__setitem__(
            "api:new_thing", {"class": "mystery", "permission": "task.read"}
        ),
    )
    with pytest.raises(PolicyCatalogError) as exc:
        PolicyCatalog(pol, SCHEMA_DIR)
    assert exc.value.code == "POLICY_ACTION_UNCLASSIFIED"


def test_action_with_unknown_permission_is_rejected(tmp_path: Path) -> None:
    pol = _copy_policy(tmp_path)
    _edit(
        pol / "risk-rules.yaml",
        lambda d: d["actions"].__setitem__(
            "api:new_thing", {"class": "read_query", "permission": "task.nope"}
        ),
    )
    with pytest.raises(PolicyCatalogError) as exc:
        PolicyCatalog(pol, SCHEMA_DIR)
    assert exc.value.code == "POLICY_PERMISSION_UNKNOWN"


def test_schema_violation_is_rejected(tmp_path: Path) -> None:
    pol = _copy_policy(tmp_path)
    _edit(
        pol / "risk-rules.yaml",
        lambda d: d["action_classes"]["destructive"].__setitem__("risk", "SEVERE"),
    )
    with pytest.raises(PolicyCatalogError) as exc:
        PolicyCatalog(pol, SCHEMA_DIR)
    assert exc.value.code == "POLICY_SCHEMA_INVALID"


def test_lint_detects_wrong_quorum_and_missing_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pol = _copy_policy(tmp_path)
    _edit(
        pol / "risk-rules.yaml",
        lambda d: d["approval_defaults"]["quorum"].__setitem__("CRITICAL", 1),
    )
    _edit(pol / "risk-rules.yaml", lambda d: d["actions"].pop("tool:work_poll"))
    monkeypatch.setattr(policy_lint, "PolicyCatalog", lambda: PolicyCatalog(pol, SCHEMA_DIR))
    problems, counts = policy_lint.lint()
    assert any("quorum" in p for p in problems)
    assert "unclassified action: tool:work_poll" in problems
    assert counts["unclassified"] == 1
