from __future__ import annotations

from pathlib import Path

import pytest

from server import paths


def test_project_root_finds_checkout_resources() -> None:
    paths.project_root.cache_clear()
    root = paths.project_root()
    assert (root / "schemas").is_dir()
    assert (root / "policy").is_dir()
    assert paths.schemas_path("api").is_dir()
    assert paths.policy_path("pricing.yaml").is_file()


def test_project_root_respects_resource_root_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "schemas").mkdir()
    (tmp_path / "policy").mkdir()
    monkeypatch.setenv("AGENT_COLAB_RESOURCE_ROOT", str(tmp_path))
    paths.project_root.cache_clear()
    try:
        assert paths.project_root() == tmp_path.resolve()
        assert paths.schemas_path() == tmp_path.resolve() / "schemas"
    finally:
        paths.project_root.cache_clear()
