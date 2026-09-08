from tools import name_role_lint


def test_runner_catalog_is_not_a_core_role(tmp_path, monkeypatch):
    catalog = tmp_path / "server/agents/runner_catalog.py"
    catalog.parent.mkdir(parents=True)
    catalog.write_text('KINDS = {"codex": ("codex", "exec", "-")}\n')
    monkeypatch.setattr(name_role_lint, "ROOT", tmp_path)
    assert name_role_lint.main() == 0
    role = tmp_path / "server/roles.py"
    role.write_text('CORE_ROLE = "codex"\n')
    assert name_role_lint.main() == 1
