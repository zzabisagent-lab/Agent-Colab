"""P4-05 unit rules: master key loading (owner-only file, env), wrap/unwrap, health probe,
value-free errors."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from server.secrets import local_provider as lp
from server.secrets.envelope import new_master_key
from server.secrets.provider import SecretError, provider_for, provider_names


def test_master_key_from_env_and_owner_only_file(tmp_path: Path) -> None:
    key = new_master_key()
    assert lp.load_master_key({"AGENT_COLAB_MASTER_KEY_B64": key}).key_id == "mk-local-1"
    file = tmp_path / "master.key"
    file.write_text(key)
    os.chmod(file, 0o600)
    loaded = lp.load_master_key(
        {"AGENT_COLAB_MASTER_KEY_FILE": str(file), "AGENT_COLAB_MASTER_KEY_ID": "mk-file"}
    )
    assert loaded.key_id == "mk-file" and len(loaded.key) == 32
    os.chmod(file, 0o644)
    with pytest.raises(SecretError) as exc:
        lp.load_master_key({"AGENT_COLAB_MASTER_KEY_FILE": str(file)})
    assert exc.value.code == "SECRET_PROVIDER_UNAVAILABLE" and "0600" in exc.value.detail


def test_missing_or_invalid_master_key_is_unavailable_without_value_details() -> None:
    for env in ({}, {"AGENT_COLAB_MASTER_KEY_B64": base64.b64encode(b"short").decode()}):
        with pytest.raises(SecretError) as exc:
            lp.load_master_key(env)
        assert exc.value.code == "SECRET_PROVIDER_UNAVAILABLE"
        assert "short" not in str(exc.value)


def test_wrap_unwrap_roundtrip_and_wrong_key_fails() -> None:
    master = lp.MasterKey.from_b64("a", new_master_key())
    other = lp.MasterKey.from_b64("b", new_master_key())
    dek = os.urandom(32)
    wrapped = lp._wrap(master, dek, "dek://x")
    assert lp._unwrap(master, wrapped, "dek://x") == dek
    with pytest.raises(InvalidTag):
        lp._unwrap(other, wrapped, "dek://x")
    with pytest.raises(InvalidTag):  # the dek_id is authenticated data
        lp._unwrap(master, wrapped, "dek://y")


def test_provider_registered_and_errors_are_stable() -> None:
    assert "local" in provider_names()
    with pytest.raises(SecretError) as exc:
        provider_for("nope", {})
    assert exc.value.code == "SECRET_PROVIDER_UNAVAILABLE"
    with pytest.raises(ValueError):
        SecretError("NOT_A_CODE")
