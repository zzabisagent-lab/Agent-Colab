"""In-process fake Secret Broker implementing docs/protocol/secret-sidecar-api.md as a WSGI app.

Usable through ``httpx.WSGITransport`` (unit tests) or served over loopback HTTP with
``serve_in_thread`` (CLI tests). It never logs values either.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import secrets
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

StartResponse = Callable[[str, list[tuple[str, str]]], Any]


@dataclass
class IssuedHandle:
    handle: str
    value: bytes = field(repr=False)
    instance_id: str
    ttl_s: float
    single_use: bool = True
    used: bool = False
    revoked: bool = False
    lease_id: str | None = None


@dataclass(frozen=True)
class RevocationItem:
    seq: int
    lease_id: str
    reason: str

    def json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": "lease",
            "target_id": self.lease_id,
            "lease_ids": [self.lease_id],
            "reason": self.reason,
            "occurred_at": dt.datetime.now(dt.UTC).isoformat(),
        }


class FakeBroker:
    def __init__(self, token: str = "svc-sidecar-token-0001") -> None:  # noqa: S107 - test broker token
        self.token = token
        self.handles: dict[str, IssuedHandle] = {}
        self.leases: dict[str, IssuedHandle] = {}
        self.revocations: list[RevocationItem] = []
        self.acks: list[str] = []
        self.requests: list[str] = []
        self.stream_open = True
        self._cond = threading.Condition()

    # ------------------------------------------------------------------ control surface
    def issue(
        self, value: bytes, instance_id: str, *, ttl_s: float = 300.0, single_use: bool = True
    ) -> str:
        handle = "sh-" + secrets.token_hex(16)
        self.handles[handle] = IssuedHandle(handle, value, instance_id, ttl_s, single_use)
        return handle

    def revoke(self, lease_id: str, reason: str = "task_ended") -> RevocationItem:
        with self._cond:
            item = RevocationItem(len(self.revocations) + 1, lease_id, reason)
            self.revocations.append(item)
            issued = self.leases.get(lease_id)
            if issued is not None:
                issued.revoked = True
            self._cond.notify_all()
            return item

    def close_streams(self) -> None:
        with self._cond:
            self.stream_open = False
            self._cond.notify_all()

    # ------------------------------------------------------------------ WSGI
    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        path, method = environ.get("PATH_INFO", ""), environ.get("REQUEST_METHOD", "GET")
        self.requests.append(f"{method} {path}")
        auth = environ.get("HTTP_AUTHORIZATION", "")
        if auth != f"Bearer {self.token}":
            return self._json(start_response, 401, {"code": "AUTH_REQUIRED"})
        if method == "POST" and path == "/api/v1/secrets/resolve":
            return self._resolve(environ, start_response)
        if method == "GET" and path == "/api/v1/secrets/revocations":
            return self._poll(environ, start_response)
        if method == "GET" and path == "/api/v1/secrets/revocations/stream":
            return self._stream(environ, start_response)
        if (
            method == "POST"
            and path.startswith("/api/v1/secrets/leases/")
            and path.endswith("/ack-cleanup")
        ):
            lease_id = path.split("/")[-2]
            self.acks.append(lease_id)
            return self._json(start_response, 200, {"lease_id": lease_id, "acknowledged": True})
        return self._json(start_response, 404, {"code": "NOT_FOUND"})

    @staticmethod
    def _json(start_response: StartResponse, status: int, body: dict[str, Any]) -> list[bytes]:
        payload = json.dumps(body).encode()
        reasons = {200: "OK", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found"}
        start_response(
            f"{status} {reasons.get(status, 'OK')}",
            [("Content-Type", "application/json"), ("Content-Length", str(len(payload)))],
        )
        return [payload]

    def _resolve(self, environ: dict[str, Any], start_response: StartResponse) -> list[bytes]:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        body = json.loads(environ["wsgi.input"].read(length) or b"{}")
        issued = self.handles.get(str(body.get("handle", "")))
        if issued is None:
            return self._json(start_response, 403, {"code": "SECRET_NOT_FOUND"})
        if issued.instance_id != body.get("sidecar_instance_id"):
            return self._json(start_response, 403, {"code": "SECRET_HANDLE_HOST_MISMATCH"})
        if issued.revoked:
            return self._json(start_response, 403, {"code": "SECRET_HANDLE_REVOKED"})
        if issued.used and issued.single_use:
            return self._json(start_response, 403, {"code": "SECRET_HANDLE_USED"})
        issued.used = True
        lease_id = issued.lease_id or ("lease-" + secrets.token_hex(8))
        issued.lease_id = lease_id
        self.leases[lease_id] = issued
        return self._json(
            start_response,
            200,
            {"lease_id": lease_id, "secret_b64": base64.b64encode(issued.value).decode("ascii")},
        )

    def _pending(self, since: int) -> list[RevocationItem]:
        return [r for r in self.revocations if r.seq > since]

    def _poll(self, environ: dict[str, Any], start_response: StartResponse) -> list[bytes]:
        query = parse_qs(environ.get("QUERY_STRING", ""))
        since = int(query.get("since", ["0"])[0])
        wait_s = min(float(query.get("max_wait_s", query.get("wait_s", ["5"]))[0]), 5.0)
        with self._cond:
            if not self._pending(since):
                self._cond.wait(timeout=wait_s)
            items = self._pending(since)
        next_seq = max([since, *(i.seq for i in items)])
        return self._json(
            start_response, 200, {"items": [i.json() for i in items], "next_since": next_seq}
        )

    def _stream(self, environ: dict[str, Any], start_response: StartResponse) -> Iterator[bytes]:
        query = parse_qs(environ.get("QUERY_STRING", ""))
        since = int(query.get("since", ["0"])[0])
        start_response(
            "200 OK", [("Content-Type", "text/event-stream"), ("Cache-Control", "no-cache")]
        )
        return self._events(since)

    def _events(self, since: int) -> Iterator[bytes]:
        yield b": connected\n\n"
        while True:
            with self._cond:
                while self.stream_open and not self._pending(since):
                    self._cond.wait(timeout=0.5)
                if not self.stream_open:
                    return
                items = self._pending(since)
            for item in items:
                since = max(since, item.seq)
                payload = json.dumps(item.json())
                yield f"event: revocation\nid: {item.seq}\ndata: {payload}\n\n".encode()


class _ThreadingServer(WSGIServer):
    allow_reuse_address = True
    daemon_threads = True

    def process_request(self, request: Any, client_address: Any) -> None:  # one thread per request
        thread = threading.Thread(
            target=self.process_request_thread, args=(request, client_address), daemon=True
        )
        thread.start()

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:  # noqa: S110 - client disconnects are expected
            pass
        finally:
            self.shutdown_request(request)


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return


@dataclass
class ServedBroker:
    broker: FakeBroker
    url: str
    server: WSGIServer
    thread: threading.Thread

    def stop(self) -> None:
        self.broker.close_streams()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def serve_in_thread(broker: FakeBroker) -> ServedBroker:
    server = make_server(
        "127.0.0.1", 0, broker, server_class=_ThreadingServer, handler_class=_QuietHandler
    )
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
    )
    thread.start()
    return ServedBroker(broker, f"http://127.0.0.1:{server.server_port}", server, thread)
