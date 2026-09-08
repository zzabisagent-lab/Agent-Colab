"""Pull-mode Agent-Colab CLI runner. Commands are local operator configuration, never work data.

Subprocess security exceptions use operator argv with shell=False; work is stdin only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess  # nosec B404
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from server.agents.runner_catalog import DEFAULT_KIND, KINDS
from server.domain.clock import Clock, SystemClock

SECRET_KEY = re.compile(r"token|secret|password|passwd|api.?key|credential|authorization", re.I)


def redact(value: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            value = value.replace(secret, "<redacted>")
    value = re.sub(r"(?i)(Bearer\s+)\S+", r"\1<redacted>", value)
    return re.sub(
        r"""(?i)((?:[\w-]*(?:token|secret|password|passwd|api[_-]?key|credential)[\w-]*)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)""",
        r"\1<redacted>",
        value,
    )


@dataclass(frozen=True, repr=False)
class Config:
    base_url: str
    agent_id: str
    kind: str
    workdir: str
    token: str = field(repr=False)
    command: tuple[str, ...] = field(repr=False)
    secrets: tuple[str, ...] = field(repr=False)
    timeout: int = 120

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        base = env.get("AGENT_COLAB_BASE_URL") or env.get("AGENT_COLAB_MCP_URL", "").removesuffix(
            "/mcp"
        )
        url = urlsplit(base)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("invalid base URL (HTTP(S), no credentials/query/fragment)")
        kind = env.get("AGENT_COLAB_RUNNER_KIND", DEFAULT_KIND)
        if kind not in KINDS:
            raise ValueError("unsupported runner kind")
        agent = env.get("AGENT_COLAB_AGENT_ID", "")
        token = env.get("AGENT_COLAB_SERVICE_TOKEN", "")
        if not re.fullmatch(r"agent-[a-z0-9][a-z0-9-]{1,62}", agent) or not token:
            raise ValueError("agent id and service token required")
        override = env.get(kind.upper().replace("-", "_") + "_COMMAND")
        command = tuple(shlex.split(override)) if override is not None else KINDS[kind]
        if not command:
            raise ValueError("runner command required")
        timeout = int(env.get("AGENT_COLAB_RUNNER_TIMEOUT", "120"))
        if not 1 <= timeout <= 86400:
            raise ValueError("runner timeout out of range")
        return cls(
            base.rstrip("/"),
            agent,
            kind,
            env.get("AGENT_COLAB_WORKDIR", "."),
            token,
            command,
            tuple(v for k, v in env.items() if SECRET_KEY.search(k) and v),
            timeout,
        )

    def public(self) -> dict[str, Any]:
        return {
            "base_url": redact(self.base_url, self.secrets),
            "agent_id": self.agent_id,
            "runner_kind": self.kind,
            "workdir": redact(self.workdir, self.secrets),
            "service_token": "<redacted>",  # nosec B105
            "command": "<configured>",
            "timeout": self.timeout,
        }

    def __repr__(self) -> str:
        return json.dumps(self.public())


def build_prompt(kind: str, item: dict[str, Any]) -> str:
    return (
        f"Agent-Colab work for {kind}. Complete the supplied work and return a concise report. "
        "Never include credentials in output.\n"
        + json.dumps(item.get("payload", {}), ensure_ascii=False)
    )


def execute(config: Config, prompt: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(config.command[0])
    if executable is None:
        raise FileNotFoundError("runner command unavailable")
    env = {k: v for k, v in os.environ.items() if k != "AGENT_COLAB_SERVICE_TOKEN"}
    return subprocess.run(  # noqa: S603 -- operator-selected argv; work goes only to stdin
        [executable, *config.command[1:]],
        input=prompt,
        text=True,
        capture_output=True,
        shell=False,  # nosec B603
        cwd=config.workdir,
        env=env,
        timeout=config.timeout,
        check=False,
    )


class WorkClient(Protocol):
    def heartbeat(self) -> None: ...
    def poll(self) -> list[dict[str, Any]]: ...
    def get(self, item_id: str) -> dict[str, Any]: ...
    def ack(self, item_id: str) -> None: ...
    def start(self, item_id: str) -> None: ...
    def result(self, item_id: str, body: dict[str, Any]) -> None: ...


class RestClient:
    def __init__(self, config: Config, http: httpx.Client) -> None:
        self.config = config
        self.http = http

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.http.request(
            method,
            self.config.base_url + "/api/v1/" + path,
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Idempotency-Key": uuid.uuid4().hex,
            },
            json=body,
        )
        response.raise_for_status()
        return dict(response.json())

    def heartbeat(self) -> None:
        self.request(
            "POST",
            f"agents/{self.config.agent_id}/heartbeat",
            {"health": "ok", "capacity": 1, "usage_unavailable": "ADAPTER_NO_METERING"},
        )

    def poll(self) -> list[dict[str, Any]]:
        return list(
            self.request("POST", "work/poll", {"agent_id": self.config.agent_id, "max_items": 1})[
                "items"
            ]
        )

    def get(self, item_id: str) -> dict[str, Any]:
        return self.request("GET", f"work/{item_id}")

    def ack(self, item_id: str) -> None:
        self.request("POST", f"work/{item_id}/ack")

    def start(self, item_id: str) -> None:
        self.request("POST", f"work/{item_id}/start")

    def result(self, item_id: str, body: dict[str, Any]) -> None:
        self.request("POST", f"work/{item_id}/result", body)


class Runner:
    def __init__(
        self,
        config: Config,
        client: WorkClient,
        executor: Callable[[Config, str], subprocess.CompletedProcess[str]] = execute,
        clock: Clock | None = None,
    ) -> None:
        self.config, self.client, self.executor = config, client, executor
        self.clock = clock or SystemClock()

    def once(self) -> int:
        self.client.heartbeat()
        items = self.client.poll()
        for envelope in items:
            item_id = str(envelope["work_item_id"])
            item = self.client.get(item_id)
            self.client.ack(item_id)
            self.client.start(item_id)
            start = self.clock.now()
            error = "RUNNER_EXIT_NONZERO"
            try:
                completed = self.executor(self.config, build_prompt(self.config.kind, item))
                code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
            except subprocess.TimeoutExpired:
                code, stdout, stderr, error = -1, "", "runner timed out", "RUNNER_TIMEOUT"
            except OSError:
                code, stdout, stderr, error = (
                    -1,
                    "",
                    "runner execution unavailable",
                    "RUNNER_UNAVAILABLE",
                )
            result: dict[str, Any] = {
                "schema_id": "colab.work-result.v1",
                "work_item_id": item_id,
                "correlation_id": item["correlation_id"],
                "task_id": item.get("task_id"),
                "status": "SUCCEEDED" if code == 0 else "FAILED",
                "result": {
                    "stdout": redact(stdout, self.config.secrets)[:64000],
                    "stderr": redact(stderr, self.config.secrets)[:16000],
                    "exit_code": code,
                    "wall_time_ms": max(0, int((self.clock.now() - start).total_seconds() * 1000)),
                },
                "events": [],
                "artifacts": [],
                "usage_unavailable": {"reason": "ADAPTER_NO_METERING"},
            }
            if code != 0:
                result["error_code"] = error
            self.client.result(item_id, result)
        return len(items)


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent-Colab pull runner")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--print-config", "--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        config = Config.from_env(os.environ)
        if args.print_config:
            print(json.dumps(config.public(), indent=2))
            return 0
        with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as http:
            runner = Runner(config, RestClient(config, http))
            while True:
                runner.once()
                if args.once:
                    return 0
                time.sleep(10)
    except KeyboardInterrupt:
        return 0
    except Exception:
        # HTTP/CLI exceptions may embed credentials, URLs or output. Never print them.
        print("Agent-Colab runner failed; check configuration and server work status.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
