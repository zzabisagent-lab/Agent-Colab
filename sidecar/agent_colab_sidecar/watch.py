"""Revocation watcher: SSE push preferred, 5-second long-poll fallback (§9.4).

On a revocation the lease is removed from the store (value zeroed, injectors invalidated) and the
cleanup is acknowledged to the Broker. Expired leases are cleaned on every cycle as well.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from .client import BrokerClient, Revocation
from .errors import SidecarError
from .store import SecretStore

log = logging.getLogger("agent_colab_sidecar.watch")


class RevocationWatcher:
    def __init__(
        self,
        client: BrokerClient,
        store: SecretStore,
        *,
        poll_interval_s: float = 5.0,
        prefer_sse: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        on_revoked: Callable[[Revocation], None] | None = None,
    ) -> None:
        self.client, self.store = client, store
        self.poll_interval_s = min(max(poll_interval_s, 0.1), 5.0)
        self.prefer_sse, self._sleep, self.on_revoked = prefer_sse, sleep, on_revoked
        self.last_seq = 0
        self.sse_failures = 0
        self.handled: list[Revocation] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ handling
    def handle(self, revocation: Revocation) -> bool:
        self.last_seq = max(self.last_seq, revocation.seq)
        live = self.store.revoke(revocation.lease_id, revocation.reason or "revoked")
        self.handled.append(revocation)
        if live:
            try:
                self.client.ack_cleanup(revocation.lease_id)
            except SidecarError as exc:
                log.warning("ack-cleanup %s failed: %s", revocation.lease_id, exc.code)
        if self.on_revoked is not None:
            self.on_revoked(revocation)
        return live

    def poll_once(self) -> int:
        items, next_seq = self.client.poll_revocations(self.last_seq, wait_s=self.poll_interval_s)
        for item in items:
            self.handle(item)
        self.last_seq = max(self.last_seq, next_seq)
        self.store.expire()
        return len(items)

    def _stream(self) -> None:
        for revocation in self.client.stream_revocations(self.last_seq):
            self.handle(revocation)
            self.store.expire()
            if self._stop.is_set():
                return

    # ------------------------------------------------------------------ loop
    def run(self) -> None:
        while not self._stop.is_set():
            if self.prefer_sse:
                try:
                    self._stream()  # returns when the broker closes the stream
                except SidecarError as exc:
                    if self._stop.is_set():
                        return
                    self.sse_failures += 1
                    log.warning("revocation stream unavailable (%s): polling", exc.code)
            # stream closed or unavailable: one long-poll (≤ 5 s, returns early on events)
            # catches anything missed, then the stream is retried
            try:
                self.poll_once()
            except SidecarError as exc:
                if self._stop.is_set():
                    return
                log.warning("revocation poll failed (%s)", exc.code)
                self.store.expire()
                self._sleep(self.poll_interval_s)

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="sidecar-watch", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0, *, close_client: bool = False) -> None:
        """Stop the loop; ``close_client`` aborts a blocking stream read (used at shutdown)."""
        self._stop.set()
        if close_client:
            self.client.close()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
