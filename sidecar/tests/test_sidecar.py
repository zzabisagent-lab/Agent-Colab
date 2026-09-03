"""V-P4-31: injection (env/fd/socket), one-time resolve, host binding, revoke via SSE and poll
within 5 s, expiry, disk and log inspection, packaging/CLI."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from sidecar.agent_colab_sidecar import cli
from sidecar.agent_colab_sidecar.client import BrokerClient
from sidecar.agent_colab_sidecar.config import SidecarConfig, load_instance_id
from sidecar.agent_colab_sidecar.errors import SidecarError
from sidecar.agent_colab_sidecar.inject import EnvInjector, FdInjector, SocketInjector
from sidecar.agent_colab_sidecar.safelog import SafeLogFilter, redact
from sidecar.agent_colab_sidecar.store import SecretStore
from sidecar.agent_colab_sidecar.watch import RevocationWatcher
from sidecar.tests import child
from sidecar.tests.fake_broker import FakeBroker, serve_in_thread

CHILD = [sys.executable, str(Path(child.__file__).resolve())]
VALUE = b"hunter2-correct-horse-battery-staple-0042"
VALUE_B64 = base64.b64encode(VALUE).decode()
VALUE_HEX = VALUE.hex()
EXPECTED_HMAC = "hmac=" + hmac.new(child.KEY, VALUE, hashlib.sha256).hexdigest()
TOKEN = "svc-sidecar-token-0001"  # noqa: S105 - test token


def _config(tmp_path: Path, broker_url: str = "http://broker.test") -> SidecarConfig:
    return SidecarConfig.from_env(
        {
            "AGENT_COLAB_SIDECAR_BROKER_URL": broker_url,
            "AGENT_COLAB_SIDECAR_TOKEN": TOKEN,
            "AGENT_COLAB_SIDECAR_RUNTIME_DIR": str(tmp_path / "run"),
            "AGENT_COLAB_SIDECAR_POLL_INTERVAL_S": "0.3",
        }
    )


@pytest.fixture
def broker() -> Iterator[FakeBroker]:
    fb = FakeBroker(TOKEN)
    yield fb
    fb.close_streams()


@pytest.fixture
def config(tmp_path: Path) -> SidecarConfig:
    return _config(tmp_path)


@pytest.fixture
def client(config: SidecarConfig, broker: FakeBroker) -> Iterator[BrokerClient]:
    c = BrokerClient(config, transport=httpx.WSGITransport(app=broker))
    yield c
    c.close()


def _resolve(
    client: BrokerClient, store: SecretStore, broker: FakeBroker, config: SidecarConfig
) -> str:
    handle = broker.issue(VALUE, config.instance_id)
    lease = client.resolve(handle)
    store.put(lease.lease_id, handle, lease.value, 300)
    return lease.lease_id


def _wait(predicate: object, timeout_s: float = 5.0) -> float:
    """Bounded real wait; returns the elapsed seconds (must stay ≤ 5 s per §9.4)."""
    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        if predicate():  # type: ignore[operator]
            return time.monotonic() - start
        time.sleep(0.02)
    return time.monotonic() - start


def _no_value_in(blob: bytes | str) -> None:
    text = blob.decode("utf-8", "replace") if isinstance(blob, bytes) else blob
    assert VALUE.decode() not in text and VALUE_B64 not in text and VALUE_HEX not in text


# ------------------------------------------------------------------ resolve / host binding
def test_resolve_once_and_second_resolve_rejected(
    client: BrokerClient, broker: FakeBroker, config: SidecarConfig
) -> None:
    handle = broker.issue(VALUE, config.instance_id)
    lease = client.resolve(handle, work_item_id="wi-0001")
    assert bytes(lease.value) == VALUE and lease.lease_id.startswith("lease-")
    assert "hunter2" not in repr(lease)
    with pytest.raises(SidecarError) as second:
        client.resolve(handle)
    assert second.value.code == "SECRET_HANDLE_USED"
    assert VALUE.decode() not in str(second.value)


def test_other_host_handle_rejected(client: BrokerClient, broker: FakeBroker) -> None:
    handle = broker.issue(VALUE, "sc-another-host-000000000000")
    with pytest.raises(SidecarError) as exc:
        client.resolve(handle)
    assert exc.value.code == "SECRET_HANDLE_HOST_MISMATCH"
    unknown = "sh-" + "0" * 32
    with pytest.raises(SidecarError) as nf:
        client.resolve(unknown)
    assert nf.value.code == "SECRET_NOT_FOUND"


def test_bad_token_is_auth_failure(config: SidecarConfig, broker: FakeBroker) -> None:
    bad = BrokerClient(
        SidecarConfig(config.broker_url, config.instance_id, token="wrong"),  # noqa: S106
        transport=httpx.WSGITransport(app=broker),
    )
    with pytest.raises(SidecarError) as exc:
        bad.resolve(broker.issue(VALUE, config.instance_id))
    assert exc.value.code == "BROKER_AUTH_FAILED"


# ------------------------------------------------------------------ injection modes
def test_env_injection_child_receives_value(
    client: BrokerClient, broker: FakeBroker, config: SidecarConfig
) -> None:
    store = SecretStore()
    lease_id = _resolve(client, store, broker, config)
    inj = EnvInjector(
        store,
        lease_id,
        CHILD,
        env_name="MY_SECRET",
        stdout=subprocess.PIPE,
        base_env={**os.environ, "SECRET_ENV_NAME": "MY_SECRET"},
    )
    proc = inj.start()
    out, _ = proc.communicate(timeout=10)
    assert out.decode().strip() == EXPECTED_HMAC
    _no_value_in(out)
    store.wipe_all()


@pytest.mark.parametrize("use_memfd", [True, False])
def test_fd_injection_child_receives_value(
    client: BrokerClient, broker: FakeBroker, config: SidecarConfig, use_memfd: bool
) -> None:
    store = SecretStore()
    lease_id = _resolve(client, store, broker, config)
    inj = FdInjector(store, lease_id, CHILD, use_memfd=use_memfd, stdout=subprocess.PIPE)
    proc = inj.start()
    out, _ = proc.communicate(timeout=10)
    assert out.decode().strip() == EXPECTED_HMAC
    assert inj.memfd is use_memfd
    inj.invalidate("done")
    store.wipe_all()


def test_socket_injection_serves_once_to_local_owner(
    client: BrokerClient, broker: FakeBroker, config: SidecarConfig
) -> None:
    store = SecretStore()
    lease_id = _resolve(client, store, broker, config)
    assert config.runtime_dir is not None
    path = config.runtime_dir / "s.sock"
    inj = SocketInjector(store, lease_id, path)
    store.attach(lease_id, inj)
    inj.start()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(path))
        received = b""
        while chunk := s.recv(4096):
            received += chunk
    assert received == VALUE
    assert _wait(lambda: inj.served and not path.exists()) < 5
    with pytest.raises(OSError):  # single use: the socket is gone
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as again:
            again.connect(str(path))
    store.wipe_all()


# ------------------------------------------------------------------ revocation
def _holding_child(store: SecretStore, lease_id: str, respawn: bool = False) -> EnvInjector:
    inj = EnvInjector(
        store,
        lease_id,
        [*CHILD, "--hold"],
        env_name="MY_SECRET",
        respawn_without=respawn,
        stdout=subprocess.PIPE,
        base_env={**os.environ, "SECRET_ENV_NAME": "MY_SECRET"},
    )
    store.attach(lease_id, inj)
    inj.start()
    return inj


@pytest.mark.parametrize("prefer_sse", [True, False])
def test_revocation_clears_memory_and_child_within_5s(
    client: BrokerClient, broker: FakeBroker, config: SidecarConfig, prefer_sse: bool
) -> None:
    store = SecretStore()
    handle = broker.issue(VALUE, config.instance_id)
    lease = client.resolve(handle)
    buffer = lease.value  # keep a reference to observe zeroing
    store.put(lease.lease_id, handle, buffer, 300)
    inj = _holding_child(store, lease.lease_id, respawn=True)
    watcher = RevocationWatcher(client, store, poll_interval_s=0.3, prefer_sse=prefer_sse)
    watcher.start()
    try:
        assert inj.process is not None and inj.process.poll() is None
        broker.revoke(lease.lease_id, "task_ended")
        elapsed = _wait(
            lambda: lease.lease_id not in store.lease_ids() and inj.process.poll() is not None
        )
        assert elapsed < 5.0, (
            f"revocation applied after {elapsed:.2f} s; handled={watcher.handled} "
            f"sse_failures={watcher.sse_failures} requests={broker.requests[-4:]} "
            f"leases={store.lease_ids()} child_rc={inj.process.poll()} "
            f"thread_alive={watcher._thread is not None and watcher._thread.is_alive()}"
        )
        assert len(buffer) == 0  # zeroed then released
        assert inj.process.poll() is not None  # child with the value is gone
        assert _wait(lambda: inj.respawned is not None) < 5
        assert inj.respawned is not None and inj.respawned.stdout is not None
        first_line = inj.respawned.stdout.readline().decode().strip()
        assert first_line == "no-secret"  # re-spawned without the value
        inj.respawned.terminate()
        inj.respawned.wait(timeout=5)
        assert _wait(lambda: lease.lease_id in broker.acks) < 5  # cleanup acknowledged
        assert watcher.sse_failures == 0 if prefer_sse else True
    finally:
        watcher.stop()
        store.wipe_all()


def test_sse_falls_back_to_polling_when_stream_unavailable(
    config: SidecarConfig, broker: FakeBroker
) -> None:
    broker.close_streams()  # the stream endpoint ends immediately → watcher polls
    client = BrokerClient(config, transport=httpx.WSGITransport(app=broker))
    store = SecretStore()
    handle = broker.issue(VALUE, config.instance_id)
    lease = client.resolve(handle)
    store.put(lease.lease_id, handle, lease.value, 300)
    watcher = RevocationWatcher(client, store, poll_interval_s=0.3, prefer_sse=True)
    watcher.start()
    try:
        broker.revoke(lease.lease_id)
        assert _wait(lambda: lease.lease_id not in store.lease_ids()) < 5.0
    finally:
        watcher.stop()
        client.close()


def test_expiry_invalidates_injectors(
    client: BrokerClient, broker: FakeBroker, config: SidecarConfig
) -> None:
    store = SecretStore()
    handle = broker.issue(VALUE, config.instance_id)
    lease = client.resolve(handle)
    store.put(lease.lease_id, handle, lease.value, ttl_s=0.2)
    inj = _holding_child(store, lease.lease_id)
    time.sleep(0.25)
    assert store.expire() == [lease.lease_id]
    assert inj.process is not None and inj.process.poll() is not None
    with pytest.raises(SidecarError) as exc:
        store.view(lease.lease_id)
    assert exc.value.code == "LEASE_UNKNOWN"


def test_store_is_never_serialized() -> None:
    import pickle

    with pytest.raises(TypeError):
        pickle.dumps(SecretStore())


# ------------------------------------------------------------------ disk and log inspection
def test_runtime_dir_holds_only_instance_id_and_no_value_bytes(
    client: BrokerClient, broker: FakeBroker, config: SidecarConfig, tmp_path: Path
) -> None:
    store = SecretStore()
    lease_id = _resolve(client, store, broker, config)
    inj = FdInjector(store, lease_id, CHILD, stdout=subprocess.PIPE)
    proc = inj.start()
    proc.communicate(timeout=10)
    inj.invalidate("done")
    store.wipe_all()
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert [p.name for p in files] == ["instance-id"]
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert files[0].read_text().strip() == config.instance_id
    for path in files:
        _no_value_in(path.read_bytes())
    assert config.instance_id == load_instance_id(config.runtime_dir)  # stable across loads


def test_logs_only_carry_ids_and_outcomes(
    client: BrokerClient,
    broker: FakeBroker,
    config: SidecarConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.handler.addFilter(SafeLogFilter())
    with caplog.at_level(logging.DEBUG):
        store = SecretStore()
        handle = broker.issue(VALUE, config.instance_id)
        lease = client.resolve(handle)
        store.put(lease.lease_id, handle, lease.value, 300)
        inj = _holding_child(store, lease.lease_id)
        logging.getLogger("third.party").info("secret_b64=%s len=%d", VALUE_B64, len(VALUE))
        store.revoke(lease.lease_id, "revoked")
        assert inj.process is not None and inj.process.poll() is not None
    text = "\n".join(r.getMessage() for r in caplog.records)
    _no_value_in(text)
    assert str(len(VALUE)) not in text.replace(lease.lease_id, "").replace(handle, "")
    assert hashlib.sha256(VALUE).hexdigest() not in text
    assert handle in text and lease.lease_id in text and "buffer zeroed" in text


def test_redaction_rules() -> None:
    assert redact("resolve sh-00112233445566778899aabbccddeeff ok lease=lease-0123456789ab") == (
        "resolve sh-00112233445566778899aabbccddeeff ok lease=lease-0123456789ab"
    )
    out = redact(
        f"token={VALUE_B64} authorization: Bearer abc len=41 {hashlib.sha256(VALUE).hexdigest()}"
    )
    assert VALUE_B64 not in out and "41" not in out and "[redacted]" in out


def test_config_never_exposes_token(config: SidecarConfig) -> None:
    assert TOKEN not in repr(config) and TOKEN not in str(config.describe())
    assert config.auth_method == "service_token" and config.poll_interval_s <= 5.0
    with pytest.raises(SidecarError) as exc:
        SidecarConfig.from_env({"AGENT_COLAB_SIDECAR_BROKER_URL": "http://b"})
    assert exc.value.code == "CONFIG_INVALID"


# ------------------------------------------------------------------ CLI end to end
def _cli_env(url: str, tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "AGENT_COLAB_SIDECAR_BROKER_URL": url,
        "AGENT_COLAB_SIDECAR_TOKEN": TOKEN,
        "AGENT_COLAB_SIDECAR_RUNTIME_DIR": str(tmp_path / "run"),
        "AGENT_COLAB_SIDECAR_POLL_INTERVAL_S": "0.3",
        "SECRET_ENV_NAME": "MY_SECRET",
    }


def test_cli_run_and_revoke_over_real_http(tmp_path: Path) -> None:
    broker = FakeBroker(TOKEN)
    served = serve_in_thread(broker)
    try:
        env = _cli_env(served.url, tmp_path)
        instance = load_instance_id(tmp_path / "run")
        status = subprocess.run(
            [sys.executable, "-m", "sidecar.agent_colab_sidecar", "status"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        assert instance in status.stdout and TOKEN not in status.stdout + status.stderr
        # env injection through the CLI: the child prints the HMAC and exits 0
        handle = broker.issue(VALUE, instance)
        run = subprocess.run(  # noqa: S603 - argv list
            [
                sys.executable,
                "-m",
                "sidecar.agent_colab_sidecar",
                "run",
                "--handle",
                handle,
                "--mode",
                "env",
                "--env-name",
                "MY_SECRET",
                "--",
                *CHILD,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert run.returncode == 0, run.stderr[-800:]
        assert EXPECTED_HMAC in run.stdout
        _no_value_in(run.stdout + run.stderr)
        # a holding child is torn down within 5 s of a revocation; exit code 3
        handle2 = broker.issue(VALUE, instance)
        proc = subprocess.Popen(  # noqa: S603 - argv list
            [
                sys.executable,
                "-m",
                "sidecar.agent_colab_sidecar",
                "run",
                "--handle",
                handle2,
                "--mode",
                "fd",
                "--",
                *CHILD,
                "--hold",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert (
            _wait(lambda: any(h.lease_id for h in broker.handles.values() if h.handle == handle2))
            < 10
        )
        lease_id = broker.handles[handle2].lease_id
        assert lease_id is not None
        time.sleep(0.5)  # let the watcher connect
        revoked_at = time.monotonic()
        broker.revoke(lease_id)
        out, err = proc.communicate(timeout=15)
        took = time.monotonic() - revoked_at
        assert took < 5.0 + 1.0, f"exit {took:.2f} s after revoke; sidecar log:\n{err[-1500:]}"
        assert proc.returncode == cli.EXIT_REVOKED, err[-800:]
        _no_value_in(out + err)
        assert lease_id in broker.acks
    finally:
        served.stop()
