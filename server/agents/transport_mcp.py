"""MCP server transport for Agent work delivery (development plan §7B.2/§7B.3, §7.4; P3-10).

Adds to the core MCP tool surface:

* ``work_poll`` — pull delivery with a long-poll (``max_wait_s`` ≤ 30 s, answered within 30 s),
  one concurrent poll per MCP session (``MCP_POLL_IN_PROGRESS``); on reconnect, DELIVERED items
  that were never ACKED are returned again (``inbox.poll`` semantics).
* ``work_ack`` / ``work_start`` / ``work_reject`` / ``work_result`` (idempotent; duplicate
  results are ignored and audited by the inbox core), ``usage_report``, ``artifact_register``,
  ``verification_submit``, ``verification_evidence_submit`` — all bus commands.
* ``task_get`` / ``document_get`` — read queries (workspace scoped, permission checked).
* Resources ``colab://inbox/{agent_id}`` (caller's own inbox only), ``colab://task/{task_id}``,
  ``colab://document/{document_id}``; ``resources/subscribe`` on an inbox yields
  ``notifications/resources/updated`` whenever the inbox gains a deliverable item.
* mTLS: TLS is terminated at the reverse proxy (deployed in Phase 5). The proxy forwards the
  verified client-certificate fingerprint in ``AGENT_COLAB_MTLS_HEADER`` together with the shared
  proxy secret in ``AGENT_COLAB_MTLS_PROXY_SECRET_HEADER``; :class:`MtlsProxyMiddleware` turns
  that into a one-time ``Bearer mtls:<nonce>`` token resolved against ``agents.endpoint
  ->>'mtls_fingerprint'``. Without the proxy secret the header is ignored (401).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import json
import logging
import os
import secrets
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mcp import types
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ResourceError
from mcp.server.subscriptions import ResourceUpdated
from sqlalchemy import text

from server.api.dispatch import Runtime, execute_command
from server.api.errors import ApiError
from server.application import work as wk
from server.db.engine import session_scope
from server.identity.principals import Principal
from server.work import inbox, notify

log = logging.getLogger(__name__)

LONG_POLL_MAX_S = 30.0
SAFETY_MARGIN_S = 0.5  # the answer leaves the server before the client's 30 s budget ends
POLL_INTERVAL_S = 0.25
SCHEMA_ID_BASE = "https://agent-colab.dev/schemas/api/mcp"
INBOX_URI = "colab://inbox/{agent_id}"
TASK_URI = "colab://task/{task_id}"
DOCUMENT_URI = "colab://document/{document_id}"
MTLS_HEADER_ENV = "AGENT_COLAB_MTLS_HEADER"
MTLS_PROXY_SECRET_ENV = "AGENT_COLAB_MTLS_PROXY_SECRET"  # noqa: S105 - env var name  # nosec B105 - environment variable name
MTLS_PROXY_SECRET_HEADER = "x-agent-colab-proxy-auth"  # noqa: S105 - header name  # nosec B105 - header name


def _error(code: str, status: int, detail: str) -> dict[str, Any]:
    return {
        "error": {"code": code, "status": status, "detail": detail},
        "schema_id": f"{SCHEMA_ID_BASE}/error.v1",
    }


# ------------------------------------------------------------------ inbox subscriptions


@dataclass
class InboxSubscriptions:
    """Sessions subscribed per resource URI; notifications are scheduled on the server loop."""

    sessions: dict[str, set[Any]] = field(default_factory=dict)
    loop: asyncio.AbstractEventLoop | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    published: list[str] = field(default_factory=list)  # for tests/evidence

    def subscribe(self, uri: str, session: Any) -> None:
        self.capture_loop()
        with self.lock:
            self.sessions.setdefault(uri, set()).add(session)
        log.debug("inbox subscription %s (%d sessions)", uri, len(self.sessions[uri]))

    def unsubscribe(self, uri: str, session: Any) -> None:
        with self.lock:
            self.sessions.get(uri, set()).discard(session)

    bus: Any = None  # the MCPServer subscription bus (``subscriptions/listen`` streams)

    def capture_loop(self) -> None:
        """Remember the server event loop (called from any request handler)."""
        if self.loop is None:
            try:
                self.loop = asyncio.get_running_loop()
            except RuntimeError:  # pragma: no cover - always called from the loop
                self.loop = None

    def notify(self, uri: str) -> None:
        with self.lock:
            targets = list(self.sessions.get(uri, ()))
            loop = self.loop
        log.debug("inbox change %s: %d legacy sessions", uri, len(targets))
        if loop is None:
            return
        self.published.append(uri)
        bus = self.bus

        async def _send() -> None:
            if bus is not None:  # 2026-07-28 protocol: ``subscriptions/listen`` streams
                try:
                    await bus.publish(ResourceUpdated(uri=uri))
                except Exception:
                    log.debug("bus publish failed for %s", uri, exc_info=True)
            for session in targets:  # legacy ``resources/subscribe`` sessions
                try:
                    await session.send_resource_updated(uri)
                except Exception:  # a dead session must not stop the others
                    log.debug("resource update not delivered for %s", uri, exc_info=True)

        def _schedule() -> None:
            loop.create_task(_send())

        try:
            loop.call_soon_threadsafe(_schedule)
        except RuntimeError:  # loop closed while shutting down
            log.debug("resource update skipped for %s: loop closed", uri)


# ------------------------------------------------------------------------ mTLS (proxy)


@dataclass
class MtlsNonces:
    """One-time nonces minted by the proxy middleware and consumed by the token verifier."""

    pending: dict[str, str] = field(default_factory=dict)  # nonce -> certificate fingerprint
    lock: threading.Lock = field(default_factory=threading.Lock)

    def mint(self, fingerprint: str) -> str:
        nonce = secrets.token_urlsafe(24)
        with self.lock:
            self.pending[nonce] = fingerprint
        return nonce

    def consume(self, nonce: str) -> str | None:
        with self.lock:
            return self.pending.pop(nonce, None)


MTLS_NONCES = MtlsNonces()


def mtls_enabled() -> bool:
    return bool(os.environ.get(MTLS_HEADER_ENV)) and bool(os.environ.get(MTLS_PROXY_SECRET_ENV))


class MtlsProxyMiddleware:
    """ASGI middleware: proxy-verified client certificate → one-time Bearer ``mtls:<nonce>``.

    Only requests that carry the proxy shared secret are trusted; any other request keeps its
    own Authorization header untouched (so a forged fingerprint header is worthless).
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and mtls_enabled():
            header_name = os.environ[MTLS_HEADER_ENV].lower().encode()
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            proxy_secret = headers.get(MTLS_PROXY_SECRET_HEADER.encode(), b"").decode()
            fingerprint = headers.get(header_name, b"").decode().strip()
            expected = os.environ[MTLS_PROXY_SECRET_ENV]
            if fingerprint and proxy_secret and hmac.compare_digest(proxy_secret, expected):
                nonce = MTLS_NONCES.mint(fingerprint)
                new_headers = [
                    (k, v) for k, v in scope["headers"] if k.lower() != b"authorization"
                ] + [(b"authorization", f"Bearer mtls:{nonce}".encode())]
                scope = dict(scope, headers=new_headers)
        await self.app(scope, receive, send)


def resolve_mtls_principal(session: Any, token: str) -> Principal | None:
    """``mtls:<nonce>`` → the Agent whose ``endpoint.mtls_fingerprint`` matches the certificate."""
    if not token.startswith("mtls:"):
        return None
    fingerprint = MTLS_NONCES.consume(token[len("mtls:") :])
    if fingerprint is None:
        return None
    row = session.execute(
        text(
            "SELECT a.account_id, a.id, a.account_type FROM agents g "
            "JOIN accounts a ON a.id = g.account_id "
            "WHERE g.endpoint->>'mtls_fingerprint' = :f AND g.status IN ('active','offline') "
            "AND a.status = 'ACTIVE'"
        ),
        {"f": fingerprint},
    ).first()
    if row is None:
        return None
    return Principal(
        account_id=str(row[0]),
        account_uuid=str(row[1]),
        account_type=str(row[2]),
        credential_fingerprint=f"mtls:{fingerprint}",
        credential_kind="mtls",
    )


# ------------------------------------------------------------------------- work tools


@dataclass
class PollGuard:
    """One concurrent ``work_poll`` per MCP session (§7B.3)."""

    active: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def acquire(self, key: str) -> bool:
        with self.lock:
            if key in self.active:
                return False
            self.active.add(key)
            return True

    def release(self, key: str) -> None:
        with self.lock:
            self.active.discard(key)


def _agent_id_for(runtime: Runtime, principal: Principal) -> str | None:
    with session_scope(runtime.session_factory) as session:
        row = session.execute(
            text("SELECT agent_id FROM agents WHERE account_id = :a"),
            {"a": uuid.UUID(principal.account_uuid)},
        ).first()
    return None if row is None else str(row[0])


def _run(
    runtime: Runtime,
    principal: Principal,
    command: Any,
    *,
    idempotency_key: str | None,
    correlation_id: str | None,
    name: str,
) -> dict[str, Any]:
    try:
        result = execute_command(
            runtime,
            principal,
            command,
            idempotency_key=idempotency_key or f"mcp-{uuid.uuid4().hex}",
            correlation_id=correlation_id or f"corr-{uuid.uuid4().hex[:16]}",
        )
    except ApiError as exc:
        return _error(exc.code, exc.status, exc.detail)
    return {
        "schema_id": f"{SCHEMA_ID_BASE}/{name}.result.v1",
        "resource_id": result.resource_id,
        "event_id": result.event_id,
        "aggregate_type": result.aggregate_type,
        "aggregate_seq": result.aggregate_seq,
        "replayed": result.replayed,
        **result.data,
    }


def register_work_transport(
    server: MCPServer,
    runtime: Runtime,
    principal_resolver: Callable[[], Principal],
    *,
    subscriptions: InboxSubscriptions | None = None,
    poll_guard: PollGuard | None = None,
) -> InboxSubscriptions:
    """Register work tools, read tools and resources on ``server``; returns the subscription hub."""
    subs = subscriptions or InboxSubscriptions()
    guard = poll_guard or PollGuard()

    @server.tool(name="work_poll")
    async def work_poll(
        agent_id: str,
        max_items: int = 10,
        max_wait_s: float = 0,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]  # injected by the SDK
    ) -> dict[str, Any]:
        """Pull the caller's inbox; waits up to ``max_wait_s`` (≤ 30 s) for the first item."""
        subs.capture_loop()
        principal = principal_resolver()
        session_key = _session_key(ctx, principal)
        if not guard.acquire(session_key):
            return _error("MCP_POLL_IN_PROGRESS", 429, "one concurrent work_poll per session")
        try:
            budget = max(0.0, min(float(max_wait_s), LONG_POLL_MAX_S) - SAFETY_MARGIN_S)
            started = time.monotonic()
            wake = _wake_event(subs, agent_id)
            while True:
                out = await asyncio.to_thread(
                    _run,
                    runtime,
                    principal,
                    wk.WorkPoll(agent_id=agent_id, max_items=max_items),
                    idempotency_key=None,
                    correlation_id=correlation_id,
                    name="work_poll",
                )
                if "error" in out or out.get("items"):
                    return out
                remaining = budget - (time.monotonic() - started)
                if remaining <= 0:
                    out["waited_s"] = round(time.monotonic() - started, 3)
                    return out
                wake.clear()
                try:
                    await asyncio.wait_for(wake.wait(), timeout=min(POLL_INTERVAL_S, remaining))
                except TimeoutError:
                    pass
        finally:
            guard.release(session_key)

    for name, command_type in {
        "work_ack": wk.WorkAck,
        "work_start": wk.WorkStart,
        "work_reject": wk.WorkReject,
        "work_result": wk.WorkResult,
    }.items():
        server.tool(name=name)(_command_tool(runtime, principal_resolver, name, command_type))

    @server.tool(name="task_get")
    async def task_get(task_id: str) -> dict[str, Any]:
        """Read one Task (workspace scoped; not-found and forbidden are the same 404)."""
        principal = principal_resolver()
        row = await asyncio.to_thread(_task_row, runtime, principal, task_id)
        if row is None:
            return _error("NOT_FOUND", 404, "task not found")
        return {"schema_id": f"{SCHEMA_ID_BASE}/task_get.result.v1", **row}

    @server.tool(name="document_get")
    async def document_get(document_id: str, version: int | None = None) -> dict[str, Any]:
        """Read a Document version (Markdown + manifest)."""
        principal = principal_resolver()
        doc = await asyncio.to_thread(_document, runtime, principal, document_id, version)
        if doc is None:
            return _error("NOT_FOUND", 404, "document not found")
        return {"schema_id": f"{SCHEMA_ID_BASE}/document_get.result.v1", **doc}

    @server.resource(INBOX_URI, name="inbox", mime_type="application/json")
    async def inbox_resource(agent_id: str) -> str:
        principal = principal_resolver()
        own = await asyncio.to_thread(_agent_id_for, runtime, principal)
        if own != agent_id:
            raise ResourceError("NOT_FOUND: inbox not found")
        items = await asyncio.to_thread(_open_items, runtime, agent_id)
        return json.dumps({"schema_id": "colab.inbox.v1", "agent_id": agent_id, "items": items})

    @server.resource(TASK_URI, name="task", mime_type="application/json")
    async def task_resource(task_id: str) -> str:
        principal = principal_resolver()
        row = await asyncio.to_thread(_task_row, runtime, principal, task_id)
        if row is None:
            raise ResourceError("NOT_FOUND: task not found")
        return json.dumps(row, default=str)

    @server.resource(DOCUMENT_URI, name="document", mime_type="application/json")
    async def document_resource(document_id: str) -> str:
        principal = principal_resolver()
        doc = await asyncio.to_thread(_document, runtime, principal, document_id, None)
        if doc is None:
            raise ResourceError("NOT_FOUND: document not found")
        return json.dumps(doc, default=str)

    _register_subscriptions(server, subs)
    subs.bus = getattr(server, "_subscriptions", None)
    notify.register_listener(lambda agent_id: _on_inbox_changed(subs, agent_id))
    return subs


_WAKE: dict[str, asyncio.Event] = {}


def _wake_event(subs: InboxSubscriptions, agent_id: str) -> asyncio.Event:
    event = _WAKE.get(agent_id)
    if event is None:
        event = _WAKE[agent_id] = asyncio.Event()
    return event


def _on_inbox_changed(subs: InboxSubscriptions, agent_id: str) -> None:
    subs.notify(INBOX_URI.format(agent_id=agent_id))
    event = _WAKE.get(agent_id)
    loop = subs.loop
    if event is not None and loop is not None:
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:  # loop closed while shutting down
            log.debug("inbox wake-up skipped for %s: loop closed", agent_id)


def _session_key(ctx: Any, principal: Principal) -> str:
    if ctx is not None:
        try:
            sid = ctx.session_id
            if sid:
                return f"session:{sid}"
        except Exception:  # pragma: no cover - transport without session ids
            log.debug("no MCP session id; keying the poll guard by credential")
    return f"credential:{principal.credential_fingerprint}"


def _command_tool(
    runtime: Runtime,
    principal_resolver: Callable[[], Principal],
    name: str,
    command_type: type[Any],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    if name == "work_result":

        async def work_result(
            work_item_id: str,
            result: dict[str, Any],
            idempotency_key: str | None = None,
            correlation_id: str | None = None,
        ) -> dict[str, Any]:
            """Submit the work result exactly once; duplicates are ignored and audited."""
            return await asyncio.to_thread(
                _run,
                runtime,
                principal_resolver(),
                command_type(work_item_id=work_item_id, result=result),
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                name=name,
            )

        return work_result
    if name == "work_reject":

        async def work_reject(
            work_item_id: str,
            reason_code: str,
            idempotency_key: str | None = None,
            correlation_id: str | None = None,
        ) -> dict[str, Any]:
            """Reject a delivered work item (CAPABILITY_UNSUPPORTED|CAPACITY|POLICY|OTHER)."""
            return await asyncio.to_thread(
                _run,
                runtime,
                principal_resolver(),
                command_type(work_item_id=work_item_id, reason_code=reason_code),
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                name=name,
            )

        return work_reject

    async def single(
        work_item_id: str,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            _run,
            runtime,
            principal_resolver(),
            command_type(work_item_id=work_item_id),
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            name=name,
        )

    single.__name__ = name
    single.__doc__ = (command_type.__doc__ or name).strip()
    return single


def _register_subscriptions(server: MCPServer, subs: InboxSubscriptions) -> None:
    low = server._lowlevel_server

    async def on_subscribe(ctx: Any, params: types.SubscribeRequestParams) -> types.EmptyResult:
        subs.subscribe(str(params.uri), ctx.session)
        return types.EmptyResult()

    async def on_unsubscribe(ctx: Any, params: types.UnsubscribeRequestParams) -> types.EmptyResult:
        subs.unsubscribe(str(params.uri), ctx.session)
        return types.EmptyResult()

    low.add_request_handler("resources/subscribe", types.SubscribeRequestParams, on_subscribe)
    low.add_request_handler("resources/unsubscribe", types.UnsubscribeRequestParams, on_unsubscribe)


# ------------------------------------------------------------------------------ queries


def _open_items(runtime: Runtime, agent_id: str) -> list[dict[str, Any]]:
    with session_scope(runtime.session_factory) as session:
        return [i.to_delivery() for i in inbox.open_items(session, agent_id=agent_id)]


def _task_row(runtime: Runtime, principal: Principal, task_id: str) -> dict[str, Any] | None:
    with session_scope(runtime.session_factory) as session:
        ws = runtime.resolve_workspace(session, principal.account_uuid)
        row = (
            session.execute(
                text(
                    "SELECT task_id, root_task_id, parent_task_id, title, domain, risk, status, "
                    "verification_status, delegation_depth, criteria_revision, latest_progress, "
                    "last_event_id, last_aggregate_seq FROM tasks_projection "
                    "WHERE task_id = :t AND workspace_id = :ws"
                ),
                {"t": task_id, "ws": uuid.UUID(str(ws))},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        if runtime.authorizer is not None:
            try:
                runtime.authorizer.require(
                    session, principal.account_id, "task.read", domain=str(row["domain"])
                )
            except Exception:
                return None
        return dict(row)


def _document(
    runtime: Runtime, principal: Principal, document_id: str, version: int | None
) -> dict[str, Any] | None:
    from server.documents.store import DocumentStore, DocumentStoreError

    with session_scope(runtime.session_factory) as session:
        ws = runtime.resolve_workspace(session, principal.account_uuid)
        row = (
            session.execute(
                text(
                    "SELECT document_id, doc_type, source_type, source_id, current_version, "
                    "status FROM documents WHERE document_id = :d AND workspace_id = :ws"
                ),
                {"d": document_id, "ws": uuid.UUID(str(ws))},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        if runtime.authorizer is not None:
            try:
                runtime.authorizer.require(session, principal.account_id, "document.read")
            except Exception:
                return None
        v = int(version or row["current_version"])
        try:
            markdown, manifest = DocumentStore().read_version(str(ws), document_id, v)
        except DocumentStoreError:
            markdown, manifest = "", {}
        return {**dict(row), "version": v, "markdown": markdown, "manifest": manifest}


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
