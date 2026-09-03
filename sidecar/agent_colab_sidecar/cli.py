"""``agent-colab-sidecar run|resolve|status``.

Exit codes: 0 child finished / value served, 3 lease revoked or expired while in use, 4 broker
denied the handle, 5 broker unavailable, 6 configuration error, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from .client import BrokerClient, Revocation
from .config import SidecarConfig
from .errors import BROKER_DENIAL_CODES, SidecarError
from .inject import DEFAULT_ENV_NAME, EnvInjector, FdInjector, SocketInjector
from .safelog import configure_logging
from .store import SecretStore
from .watch import RevocationWatcher

log = logging.getLogger("agent_colab_sidecar.cli")
EXIT_REVOKED, EXIT_DENIED, EXIT_BROKER, EXIT_CONFIG = 3, 4, 5, 6


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-colab-sidecar")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="resolve a handle and run a child with the value injected")
    run.add_argument("--handle", required=True)
    run.add_argument("--work-item")
    run.add_argument("--mode", choices=("env", "fd", "socket"), default="fd")
    run.add_argument("--env-name", default=DEFAULT_ENV_NAME)
    run.add_argument("--socket-path")
    run.add_argument("--respawn-without", action="store_true")
    run.add_argument("argv", nargs="*", help="child command (after --)")
    resolve = sub.add_parser("resolve", help="resolve a handle and serve it once over a socket")
    resolve.add_argument("--handle", required=True)
    resolve.add_argument("--work-item")
    resolve.add_argument("--socket-path")
    sub.add_parser("status", help="print the redacted configuration")
    return parser


def _exit_code(exc: SidecarError) -> int:
    if exc.code in BROKER_DENIAL_CODES:
        return EXIT_DENIED
    if exc.code == "CONFIG_INVALID":
        return EXIT_CONFIG
    return EXIT_BROKER


def _socket_path(config: SidecarConfig, explicit: str | None, lease_id: str) -> Path:
    if explicit:
        return Path(explicit)
    if config.runtime_dir is None:
        raise SidecarError("CONFIG_INVALID", "--socket-path or a runtime directory is required")
    return config.runtime_dir / f"{lease_id}.sock"


def run_session(
    config: SidecarConfig,
    *,
    handle: str,
    mode: str,
    argv: Sequence[str],
    work_item: str | None = None,
    env_name: str = DEFAULT_ENV_NAME,
    socket_path: str | None = None,
    respawn_without: bool = False,
    poll_s: float = 0.25,
) -> int:
    """Resolve, inject, watch; returns the process exit code (see module docstring)."""
    store = SecretStore()
    client = BrokerClient(config)
    revoked: list[Revocation] = []
    try:
        lease = client.resolve(handle, work_item_id=work_item)
        ttl = max((lease.expires_at.timestamp() - time.time()), 0.0)
        store.put(lease.lease_id, handle, lease.value, ttl)
        del lease.value
        watcher = RevocationWatcher(
            client,
            store,
            poll_interval_s=config.poll_interval_s,
            prefer_sse=config.prefer_sse,
            on_revoked=revoked.append,
        )
        if mode == "socket":
            injector: SocketInjector | EnvInjector | FdInjector = SocketInjector(
                store, lease.lease_id, _socket_path(config, socket_path, lease.lease_id)
            )
        elif mode == "env":
            if not argv:
                raise SidecarError("CONFIG_INVALID", "a child command is required for --mode env")
            injector = EnvInjector(
                store, lease.lease_id, argv, env_name=env_name, respawn_without=respawn_without
            )
        else:
            if not argv:
                raise SidecarError("CONFIG_INVALID", "a child command is required for --mode fd")
            injector = FdInjector(store, lease.lease_id, argv)
        store.attach(lease.lease_id, injector)
        injector.start()
        watcher.start()
        try:
            while True:
                if isinstance(injector, SocketInjector):
                    if injector.served:
                        return 0
                    if injector.closed or lease.lease_id not in store.lease_ids():
                        return EXIT_REVOKED
                else:
                    process = injector.process
                    if process is not None and process.poll() is not None:
                        if lease.lease_id not in store.lease_ids() or revoked:
                            return EXIT_REVOKED
                        return int(process.returncode)
                    if lease.lease_id not in store.lease_ids():
                        return EXIT_REVOKED
                time.sleep(poll_s)
        finally:
            watcher.stop(close_client=True)
    except SidecarError as exc:
        log.error("%s", exc.code)
        return _exit_code(exc)
    finally:
        store.wipe_all()
        client.close()


def main(args: Sequence[str] | None = None) -> int:
    ns = _parser().parse_args(args)
    configure_logging(getattr(logging, str(ns.log_level).upper(), logging.INFO))
    try:
        config = SidecarConfig.from_env()
    except SidecarError as exc:
        log.error("%s: %s", exc.code, exc.detail)
        return EXIT_CONFIG
    if ns.command == "status":
        print(json.dumps(config.describe(), sort_keys=True))
        return 0
    if ns.command == "resolve":
        return run_session(
            config,
            handle=ns.handle,
            mode="socket",
            argv=(),
            work_item=ns.work_item,
            socket_path=ns.socket_path,
        )
    argv = [a for a in ns.argv if a != "--"]
    return run_session(
        config,
        handle=ns.handle,
        mode=ns.mode,
        argv=argv,
        work_item=ns.work_item,
        env_name=ns.env_name,
        socket_path=ns.socket_path,
        respawn_without=ns.respawn_without,
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
