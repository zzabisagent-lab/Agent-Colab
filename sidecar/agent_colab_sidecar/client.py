"""Broker HTTP client (contract: docs/protocol/secret-sidecar-api.md).

``resolve`` returns the value as a ``bytearray`` that the caller owns and zeroes; the transient
response object is closed and dropped immediately. Nothing here logs a value, a length or a hash.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import SidecarConfig
from .errors import BROKER_DENIAL_CODES, SidecarError

log = logging.getLogger("agent_colab_sidecar.client")
RESOLVE_PATH = "/api/v1/secrets/resolve"
REVOCATIONS_PATH = "/api/v1/secrets/revocations"
STREAM_PATH = "/api/v1/secrets/revocations/stream"
DEFAULT_TTL_S = 300  # §9.3 default lease TTL when the broker omits expires_at


@dataclass
class ResolvedLease:
    lease_id: str
    handle: str
    expires_at: dt.datetime
    value: bytearray = field(repr=False)

    def __repr__(self) -> str:  # never the value
        return f"ResolvedLease(lease_id={self.lease_id!r}, handle={self.handle!r})"


@dataclass(frozen=True)
class Revocation:
    seq: int
    lease_id: str
    reason: str
    kind: str = "lease"


def _revocations_from(item: Mapping[str, Any]) -> list[Revocation]:
    """One item may list several leases (``lease_ids``) or a single ``lease_id``."""
    seq = int(item.get("seq", 0))
    reason = str(item.get("reason", ""))
    kind = str(item.get("kind", "lease"))
    ids = [str(i) for i in item.get("lease_ids", [])]
    if item.get("lease_id"):
        ids.append(str(item["lease_id"]))
    return [Revocation(seq, lease_id, reason, kind) for lease_id in ids]


class BrokerClient:
    def __init__(
        self,
        config: SidecarConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.config = config
        headers = {"User-Agent": "agent-colab-sidecar", "X-Sidecar-Instance": config.instance_id}
        if config.token:
            headers["Authorization"] = f"Bearer {config.token}"
        cert = (config.client_cert, config.client_key) if config.client_cert else None
        kwargs: dict[str, Any] = {"base_url": config.broker_url, "headers": headers}
        if cert is not None:
            kwargs["cert"] = cert
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.Client(timeout=httpx.Timeout(timeout_s, read=timeout_s), **kwargs)

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _denial(response: httpx.Response) -> SidecarError:
        try:
            code = str(response.json().get("code", ""))
        except ValueError:
            code = ""
        if response.status_code in (401,):
            return SidecarError("BROKER_AUTH_FAILED", "broker rejected the sidecar credential")
        if code in BROKER_DENIAL_CODES:
            return SidecarError(code)
        if response.status_code == 403:
            return SidecarError("SECRET_SCOPE_DENIED", "broker denied the request")
        if response.status_code >= 500:
            return SidecarError("BROKER_UNAVAILABLE", f"broker returned {response.status_code}")
        return SidecarError("BROKER_BAD_RESPONSE", f"unexpected status {response.status_code}")

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise SidecarError("BROKER_UNAVAILABLE", "broker timed out") from exc
        except (httpx.HTTPError, httpx.StreamError, RuntimeError) as exc:
            raise SidecarError("BROKER_UNAVAILABLE", type(exc).__name__) from exc

    # ------------------------------------------------------------------ API
    def resolve(
        self,
        handle: str,
        *,
        work_item_id: str | None = None,
        task_id: str | None = None,
        action: str | None = None,
        purpose: str = "adapter",
    ) -> ResolvedLease:
        body: dict[str, Any] = {
            "handle": handle,
            "sidecar_instance_id": self.config.instance_id,
            "purpose": purpose,
        }
        scopes = (("work_item_id", work_item_id), ("task_id", task_id), ("action", action))
        for key, scope_value in scopes:
            if scope_value:
                body[key] = scope_value
        response = self._request("POST", RESOLVE_PATH, json=body)
        try:
            if response.status_code != 200:
                err = self._denial(response)
                log.info("resolve %s denied: %s", handle, err.code)
                raise err
            try:
                data = response.json()
                encoded = str(data.pop("secret_b64"))
                lease_id = str(data["lease_id"])
                raw_expiry = data.get("expires_at")
                expires = (
                    dt.datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00"))
                    if raw_expiry
                    else dt.datetime.now(dt.UTC) + dt.timedelta(seconds=DEFAULT_TTL_S)
                )
            except (ValueError, KeyError, TypeError) as exc:
                raise SidecarError("BROKER_BAD_RESPONSE", "resolve response malformed") from exc
        finally:
            response.close()
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise SidecarError("BROKER_BAD_RESPONSE", "secret encoding invalid") from exc
        value = bytearray(raw)
        del raw, encoded, data
        log.info("resolve %s ok lease=%s", handle, lease_id)
        return ResolvedLease(lease_id=lease_id, handle=handle, expires_at=expires, value=value)

    def poll_revocations(self, since: int, *, wait_s: float = 5.0) -> tuple[list[Revocation], int]:
        response = self._request(
            "GET",
            REVOCATIONS_PATH,
            params={"since": since, "max_wait_s": wait_s},
            timeout=httpx.Timeout(wait_s + 5.0),
        )
        try:
            if response.status_code != 200:
                raise self._denial(response)
            data = response.json()
            items = [r for i in data.get("items", []) for r in _revocations_from(i)]
            next_seq = int(data.get("next_since", data.get("next_seq", since)))
            return items, max(next_seq, *(i.seq for i in items), since)
        except (ValueError, KeyError, TypeError) as exc:
            raise SidecarError("BROKER_BAD_RESPONSE", "revocations response malformed") from exc
        finally:
            response.close()

    def stream_revocations(self, since: int) -> Iterator[Revocation]:
        """SSE ``revocation`` events; ends when the broker closes the stream (caller reconnects)."""
        try:
            with self._client.stream(
                "GET",
                STREAM_PATH,
                params={"since": since},
                headers={"Accept": "text/event-stream"},
                timeout=httpx.Timeout(10.0, read=None),
            ) as response:
                if response.status_code != 200:
                    response.read()
                    raise self._denial(response)
                event, event_id, data_lines = "", None, []
                for line in response.iter_lines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("id:"):
                        event_id = line[3:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                    elif line == "":
                        if data_lines and event in ("", "revocation", "message"):
                            payload = dict(json.loads("\n".join(data_lines)))
                            if event_id and "seq" not in payload:
                                payload["seq"] = int(event_id)
                            yield from _revocations_from(payload)
                        event, event_id, data_lines = "", None, []
        except (httpx.HTTPError, httpx.StreamError, RuntimeError) as exc:
            raise SidecarError("BROKER_UNAVAILABLE", type(exc).__name__) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise SidecarError("BROKER_BAD_RESPONSE", "revocation event malformed") from exc

    def ack_cleanup(self, lease_id: str) -> None:
        response = self._request("POST", f"/api/v1/secrets/leases/{lease_id}/ack-cleanup")
        response.close()
        if response.status_code not in (200, 204):
            log.warning("ack-cleanup %s: broker returned %s", lease_id, response.status_code)
