"""Runner contract: fake transports and executors never need vendor credentials."""

import datetime as dt
import json
import subprocess
from unittest.mock import Mock

import pytest

from server.domain.clock import FixedClock
from server.runner import KINDS, Config, Runner, build_prompt, execute, redact
from server.work.schemas import validate


def config(kind="codex", **extra):
    return Config.from_env(
        {
            "AGENT_COLAB_BASE_URL": "http://localhost:8000",
            "AGENT_COLAB_AGENT_ID": "agent-test",
            "AGENT_COLAB_SERVICE_TOKEN": "test-service-value",
            "AGENT_COLAB_RUNNER_KIND": kind,
            "AGENT_COLAB_WORKDIR": "/tmp",
            **extra,
        }
    )


def test_config_redacts_and_precedence():
    c = config(AGENT_COLAB_MCP_URL="http://other/mcp", API_KEY="private-value")
    assert c.base_url == "http://localhost:8000"
    assert "test-service-value" not in repr(c)
    assert "private-value" not in json.dumps(c.public())
    assert redact("API_KEY=private-value Bearer opaque", c.secrets) == (
        "API_KEY=<redacted> Bearer <redacted>"
    )
    assert (
        Config.from_env(
            {
                "AGENT_COLAB_MCP_URL": "http://localhost:8000/mcp",
                "AGENT_COLAB_AGENT_ID": "agent-test",
                "AGENT_COLAB_SERVICE_TOKEN": "test-value",
            }
        ).base_url
        == "http://localhost:8000"
    )


@pytest.mark.parametrize(
    "kind", ["openclaw", "hermes", "chatgpt", "codex", "claude", "claude-code", "gemini"]
)
def test_all_kinds(kind):
    assert set(KINDS) == {
        "openclaw",
        "hermes",
        "chatgpt",
        "codex",
        "claude",
        "claude-code",
        "gemini",
    }
    key = kind.upper().replace("-", "_") + "_COMMAND"
    c = config(kind, **{key: "wrapper --noninteractive"})
    assert c.command == ("wrapper", "--noninteractive")
    prompt = build_prompt(kind, {"kind": "task", "payload": {"instructions": "hello; $(exit)"}})
    assert kind in prompt and "hello; $(exit)" in prompt and "Agent-Colab" in prompt


@pytest.mark.parametrize("code", [0, 7])
def test_result_contract(code):
    client = Mock()
    item = {"work_item_id": "wi-12345678", "correlation_id": "corr-test", "payload": {"text": "hi"}}
    client.poll.return_value = [item]
    client.get.return_value = item
    executor = Mock(
        return_value=subprocess.CompletedProcess([], code, "test-service-value", "password=hidden")
    )
    runner = Runner(config(), client, executor, FixedClock(dt.datetime(2026, 1, 1, tzinfo=dt.UTC)))
    assert runner.once() == 1
    result = client.result.call_args.args[1]
    validate("work_result", result)
    assert result["status"] == ("SUCCEEDED" if code == 0 else "FAILED")
    assert result["result"]["exit_code"] == code
    assert "test-service-value" not in json.dumps(result)
    assert "hidden" not in json.dumps(result)
    assert [c[0] for c in client.method_calls] == [
        "heartbeat",
        "poll",
        "get",
        "ack",
        "start",
        "result",
    ]


def test_executor_uses_stdin_no_shell_or_service_token(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "ok", ""))
    monkeypatch.setattr("server.runner.subprocess.run", run)
    monkeypatch.setattr("server.runner.shutil.which", lambda _: "/bin/wrapper")
    execute(config(), "untrusted; $(echo text)")
    kwargs = run.call_args.kwargs
    assert kwargs["input"] == "untrusted; $(echo text)"
    assert kwargs["shell"] is False
    assert "AGENT_COLAB_SERVICE_TOKEN" not in kwargs["env"]


def test_invalid_url_does_not_echo_secrets():
    with pytest.raises(ValueError, match="base URL") as exc:
        config(AGENT_COLAB_BASE_URL="https://user:private-value@example.com")
    assert "private-value" not in str(exc.value)


def test_native_defaults():
    expected = {
        "openclaw": ("openclaw", "agent", "exec", "--message-file", "-"),
        "hermes": (
            "hermes",
            "chat",
            "--query-file",
            "-",
            "--quiet",
            "--source",
            "agent-colab-runner",
        ),
        "chatgpt": ("agent-colab-chatgpt",),
        "codex": ("codex", "exec", "-"),
        "claude": ("claude", "-p"),
        "claude-code": ("claude", "-p"),
        "gemini": ("gemini",),
    }
    for kind, command in expected.items():
        assert config(kind).command == command


@pytest.mark.parametrize(
    "failure",
    [FileNotFoundError("secret exception"), subprocess.TimeoutExpired("secret command", 1)],
)
def test_execution_errors_are_structured(failure):
    client = Mock()
    item = {"work_item_id": "wi-12345678", "correlation_id": "corr-test", "payload": {}}
    client.poll.return_value = [item]
    client.get.return_value = item
    Runner(config(), client, Mock(side_effect=failure)).once()
    body = client.result.call_args.args[1]
    validate("work_result", body)
    assert body["status"] == "FAILED"
    assert "secret exception" not in json.dumps(body)
    assert "secret command" not in json.dumps(body)


def test_print_config_has_no_side_effects(monkeypatch, capsys):
    from server.runner import main

    monkeypatch.setattr("sys.argv", ["runner", "--print-config"])
    for key, value in {
        "AGENT_COLAB_BASE_URL": "http://localhost:8000",
        "AGENT_COLAB_AGENT_ID": "agent-test",
        "AGENT_COLAB_SERVICE_TOKEN": "local-test-credential",
    }.items():
        monkeypatch.setenv(key, value)
    http = Mock(side_effect=AssertionError("must not create HTTP client"))
    monkeypatch.setattr("server.runner.httpx.Client", http)
    assert main() == 0
    assert "local-test-credential" not in capsys.readouterr().out
    http.assert_not_called()


def test_real_executor_safe_prompt(tmp_path):
    c = config(CODEX_COMMAND="/bin/cat", AGENT_COLAB_WORKDIR=str(tmp_path))
    result = execute(c, "literal $(touch should-not-exist); password=hidden")
    assert result.returncode == 0
    assert result.stdout.startswith("literal $(touch")
    assert list(tmp_path.iterdir()) == []
