"""Injection modes (§9.4): Unix domain socket, child environment, file descriptor.

Every injector reads the value from the store at the moment of injection and can be
``invalidate``d: the socket stops serving, the child process is terminated (and optionally
re-spawned without the value), the memfd is zeroed and closed. Nothing is written to disk.
"""

from __future__ import annotations

import fcntl
import logging
import os
import signal
import socket
import stat
import struct
import subprocess  # nosec B404 - child processes are the injection target
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO, Any

from .errors import SidecarError
from .store import SecretStore

log = logging.getLogger("agent_colab_sidecar.inject")
DEFAULT_FD_ENV = "AGENT_COLAB_SECRET_FD"
DEFAULT_ENV_NAME = "AGENT_COLAB_SECRET"


def _stop_process(process: subprocess.Popen[Any] | None, grace_s: float) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=grace_s)


class SocketInjector:
    """Serve the value once to a local process connecting to a Unix domain socket."""

    def __init__(self, store: SecretStore, lease_id: str, path: Path) -> None:
        self.store, self.lease_id, self.path = store, lease_id, path
        self.served = False
        self.closed = False
        self._lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.path.exists():
            self.path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.path))
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        listener.listen(1)
        listener.settimeout(0.25)
        self._listener = listener
        self._thread = threading.Thread(target=self._serve, name="sidecar-socket", daemon=True)
        self._thread.start()
        log.info("lease %s: socket ready", self.lease_id)

    def _peer_is_local_owner(self, conn: socket.socket) -> bool:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return int(uid) == os.getuid()

    def _serve(self) -> None:
        assert self._listener is not None
        while not self.closed and not self.served:
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                if not self._peer_is_local_owner(conn):
                    log.warning("lease %s: socket peer rejected (uid mismatch)", self.lease_id)
                    continue
                with self._lock:
                    if self.served or self.closed:
                        continue
                    try:
                        view = self.store.view(self.lease_id)
                    except SidecarError:
                        continue
                    conn.sendall(view)
                    view.release()
                    self.served = True
                log.info("lease %s: value served over socket once", self.lease_id)
        self._close_listener()

    def _close_listener(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        try:
            if self.path.exists() and stat.S_ISSOCK(self.path.stat().st_mode):
                self.path.unlink()
        except OSError:
            pass

    def invalidate(self, reason: str) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
        self._close_listener()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        log.info("lease %s: socket closed (%s)", self.lease_id, reason)


def _child_env(base: Mapping[str, str] | None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.pop(DEFAULT_FD_ENV, None)
    return env


class EnvInjector:
    """Spawn a child with the value in one environment variable; on invalidation the child is
    terminated and, when ``respawn_without`` is set, started again without the variable."""

    def __init__(
        self,
        store: SecretStore,
        lease_id: str,
        argv: Sequence[str],
        *,
        env_name: str = DEFAULT_ENV_NAME,
        base_env: Mapping[str, str] | None = None,
        respawn_without: bool = False,
        grace_s: float = 1.0,
        stdout: int | IO[Any] | None = None,
    ) -> None:
        self.store, self.lease_id, self.argv = store, lease_id, list(argv)
        self.env_name, self.base_env = env_name, base_env
        self.respawn_without, self.grace_s, self.stdout = respawn_without, grace_s, stdout
        self.process: subprocess.Popen[bytes] | None = None
        self.respawned: subprocess.Popen[bytes] | None = None
        self.invalidated = False
        self._lock = threading.Lock()

    def start(self) -> subprocess.Popen[bytes]:
        env = _child_env(self.base_env)
        view = self.store.view(self.lease_id)
        try:
            try:
                env[self.env_name] = bytes(view).decode("utf-8")
            except UnicodeDecodeError:
                import base64

                env[self.env_name] = base64.b64encode(bytes(view)).decode("ascii")
                env[f"{self.env_name}_ENCODING"] = "base64"
        finally:
            view.release()
        self.process = subprocess.Popen(  # noqa: S603 - argv list, no shell  # nosec B603
            self.argv, env=env, stdin=subprocess.DEVNULL, stdout=self.stdout
        )
        del env
        log.info(
            "lease %s: child pid=%s started with env injection", self.lease_id, self.process.pid
        )
        return self.process

    def invalidate(self, reason: str) -> None:
        with self._lock:
            if self.invalidated:
                return
            self.invalidated = True
        _stop_process(self.process, self.grace_s)
        log.info("lease %s: child terminated (%s)", self.lease_id, reason)
        if self.respawn_without:
            env = _child_env(self.base_env)
            env.pop(self.env_name, None)
            env.pop(f"{self.env_name}_ENCODING", None)
            self.respawned = subprocess.Popen(  # noqa: S603 - argv list, no shell  # nosec B603
                self.argv, env=env, stdin=subprocess.DEVNULL, stdout=self.stdout
            )
            log.info("lease %s: child re-spawned without the value", self.lease_id)


class FdInjector:
    """Pass the value to a child through an inherited file descriptor (memfd, pipe fallback)."""

    def __init__(
        self,
        store: SecretStore,
        lease_id: str,
        argv: Sequence[str],
        *,
        fd_env_name: str = DEFAULT_FD_ENV,
        base_env: Mapping[str, str] | None = None,
        use_memfd: bool = True,
        grace_s: float = 1.0,
        stdout: int | IO[Any] | None = None,
    ) -> None:
        self.store, self.lease_id, self.argv = store, lease_id, list(argv)
        self.fd_env_name, self.base_env, self.use_memfd = fd_env_name, base_env, use_memfd
        self.grace_s, self.stdout = grace_s, stdout
        self.process: subprocess.Popen[bytes] | None = None
        self.fd: int | None = None
        self.memfd = False
        self.invalidated = False
        self._lock = threading.Lock()

    def _open_fd(self, view: memoryview) -> int:
        if self.use_memfd and hasattr(os, "memfd_create"):
            fd = os.memfd_create("agent-colab-secret", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            os.write(fd, view)
            os.lseek(fd, 0, os.SEEK_SET)
            fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW)
            self.memfd = True
            return fd
        read_end, write_end = os.pipe()
        os.write(write_end, view)
        os.close(write_end)
        return read_end

    def start(self) -> subprocess.Popen[bytes]:
        view = self.store.view(self.lease_id)
        try:
            self.fd = self._open_fd(view)
        finally:
            view.release()
        env = _child_env(self.base_env)
        env[self.fd_env_name] = str(self.fd)
        self.process = subprocess.Popen(  # noqa: S603 - argv list, no shell  # nosec B603
            self.argv, env=env, stdin=subprocess.DEVNULL, stdout=self.stdout, pass_fds=(self.fd,)
        )
        log.info(
            "lease %s: child pid=%s started with fd injection", self.lease_id, self.process.pid
        )
        return self.process

    def invalidate(self, reason: str) -> None:
        with self._lock:
            if self.invalidated:
                return
            self.invalidated = True
        _stop_process(self.process, self.grace_s)
        fd, self.fd = self.fd, None
        if fd is not None:
            try:
                if self.memfd:
                    size = os.fstat(fd).st_size
                    if size:
                        os.pwrite(fd, bytes(size), 0)
            finally:
                os.close(fd)
        log.info("lease %s: fd closed and child terminated (%s)", self.lease_id, reason)
