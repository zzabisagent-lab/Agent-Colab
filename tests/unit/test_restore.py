from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from tools import restore


def test_restore_requires_ledger_before_running_pg_restore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = Mock()
    monkeypatch.setattr(restore.subprocess, "run", process)
    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"synthetic")

    with pytest.raises(ValueError, match="ledger required"):
        restore.run_restore(dump, "postgresql://localhost/synthetic", None)

    process.assert_not_called()


def test_restore_rejects_malformed_ledger_before_running_pg_restore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = Mock()
    monkeypatch.setattr(restore.subprocess, "run", process)
    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"synthetic")
    ledger = tmp_path / "ledger.json"
    ledger.write_text("not-json", encoding="utf-8")

    with pytest.raises(ValueError):
        restore.run_restore(dump, "postgresql://localhost/synthetic", ledger)

    process.assert_not_called()


def test_restore_rejects_invalid_ledger_shape_before_running_pg_restore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = Mock()
    monkeypatch.setattr(restore.subprocess, "run", process)
    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"synthetic")
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"not": "a list"}), encoding="utf-8")

    with pytest.raises(ValueError, match="JSON array"):
        restore.run_restore(dump, "postgresql://localhost/synthetic", ledger)

    process.assert_not_called()
