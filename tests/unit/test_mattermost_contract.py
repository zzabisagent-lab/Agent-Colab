"""Mattermost interaction contract (P0-10 / V-P0-16).

Command grammar valid/invalid fixtures, one schema per resource/verb, card/thread/callback
schemas, and the callback security validation with stable codes.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from server.channels import commands
from server.channels.commands import (
    RESOURCES,
    SCHEMAS_DIR,
    VERB_INDEX,
    VERBS,
    CommandContext,
    CommandError,
    build_schema,
    help_text,
    parse_command,
    split_options,
    tokenize,
)
from server.channels.contract import (
    CallbackEnvelope,
    CallbackError,
    MemoryNonceStore,
    body_digest,
    sign,
    validate_callback,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "mattermost"
MM_SCHEMAS = ROOT / "schemas" / "api" / "mattermost"
VALID = yaml.safe_load((FIXTURES / "commands-valid.yaml").read_text(encoding="utf-8"))
INVALID = yaml.safe_load((FIXTURES / "commands-invalid.yaml").read_text(encoding="utf-8"))
CALLBACKS = yaml.safe_load((FIXTURES / "callbacks.yaml").read_text(encoding="utf-8"))

# the §7A.2 table, verbatim: every pair must have a schema file and a VerbSpec
TABLE_7A2: dict[str, list[str]] = {
    "task": [
        "create",
        "delegate",
        "accept",
        "reject",
        "progress",
        "submit",
        "complete",
        "cancel",
        "reassign",
        "show",
        "list",
    ],
    "approve": ["request", "grant", "reject", "show", "list"],
    "verify": ["assign", "pass", "fail", "block", "show"],
    "brainstorm": [
        "start",
        "contribute",
        "summarize",
        "decide",
        "taskify",
        "pause",
        "resume",
        "close",
        "show",
    ],
    "doc": ["show", "review", "publish"],
    "schedule": ["show", "list", "run-now", "pause", "resume", "cancel-run"],
    "link": ["start", "confirm"],
    "notify": ["mute", "unmute", "digest"],
    "help": [""],
}


def _ctx(raw: dict[str, Any] | None) -> CommandContext:
    return CommandContext(**raw) if raw else CommandContext()


# --- grammar table and schemas ---------------------------------------------------------------


def test_every_resource_verb_of_7a2_has_a_verbspec_and_a_schema_file() -> None:
    expected = {(r, v) for r, verbs in TABLE_7A2.items() for v in verbs}
    assert set(VERB_INDEX) == expected
    for spec in VERBS:
        path = SCHEMAS_DIR / spec.schema_name
        assert path.exists(), spec.schema_name
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        assert schema == build_schema(spec), f"{spec.schema_name} drifted from the VerbSpec table"
        assert schema["x-permission"] == spec.permission
    assert len(list(SCHEMAS_DIR.glob("*.v1.schema.json"))) == len(VERBS) == 45
    assert tuple(TABLE_7A2) == RESOURCES


def test_minimum_arguments_of_7a2() -> None:
    create = VERB_INDEX[("task", "create")]
    assert {f.name for f in create.fields if f.required} == {"title", "criteria"}
    for verb in ("grant", "reject"):
        assert [f.name for f in VERB_INDEX[("approve", verb)].fields if f.required] == [
            "approval_id"
        ]
    for verb in ("pass", "fail"):
        assert {f.name for f in VERB_INDEX[("verify", verb)].fields if f.required} == {"evidence"}
        assert VERB_INDEX[("verify", verb)].target == "task"
    start = VERB_INDEX[("brainstorm", "start")]
    assert {f.name for f in start.fields if f.required} == {"topic", "participants"}
    assert VERB_INDEX[("link", "start")].unlinked_allowed
    assert VERB_INDEX[("link", "confirm")].unlinked_allowed
    assert VERB_INDEX[("help", "")].unlinked_allowed
    assert sum(1 for s in VERBS if s.unlinked_allowed) == 3


# --- tokenizer ----------------------------------------------------------------------------------


def test_tokenizer_quotes_and_escapes() -> None:
    assert tokenize('task create "Write the report" --criteria "a \\"quoted\\" word" --x') == [
        "task",
        "create",
        "Write the report",
        "--criteria",
        'a "quoted" word',
        "--x",
    ]
    assert tokenize("a 'b c' d") == ["a", "b c", "d"]
    assert tokenize("   ") == []


def test_split_options_forms() -> None:
    positionals, options = split_options(
        ["task-1", "--to", "@a", "--k=v", "--flag", "--criteria", "c1", "--criteria", "c2"]
    )
    assert positionals == ["task-1"]
    assert options == {"to": ["@a"], "k": ["v"], "flag": [True], "criteria": ["c1", "c2"]}


# --- valid / invalid fixtures ---------------------------------------------------------------------


@pytest.mark.parametrize("case", VALID, ids=[c["name"] for c in VALID])
def test_valid_commands(case: dict[str, Any]) -> None:
    parsed = parse_command(case["text"], _ctx(case.get("context")))
    expect = case["expect"]
    for key in ("resource", "verb", "permission", "target_kind", "target_id", "target_source"):
        if key in expect:
            assert getattr(parsed, key) == expect[key], key
    if "args" in expect:
        for k, v in expect["args"].items():
            assert parsed.args.get(k) == v, k
        if not expect["args"]:
            assert parsed.args == {}


@pytest.mark.parametrize("case", INVALID, ids=[c["name"] for c in INVALID])
def test_invalid_commands_give_stable_errors(case: dict[str, Any]) -> None:
    with pytest.raises(CommandError) as exc:
        parse_command(case["text"], _ctx(case.get("context")))
    assert exc.value.code == case["code"]
    assert exc.value.example.startswith("/colab")
    assert exc.value.message_key.startswith("command.")


def test_fixture_coverage_is_complete() -> None:
    covered = set()
    for case in VALID:
        parsed = parse_command(case["text"], _ctx(case.get("context")))
        covered.add((parsed.resource, parsed.verb))
    assert covered == set(VERB_INDEX), set(VERB_INDEX) - covered
    assert len(INVALID) >= 25
    codes = {c["code"] for c in INVALID}
    assert codes == {
        "COMMAND_PREFIX_MISSING",
        "COMMAND_RESOURCE_UNKNOWN",
        "COMMAND_VERB_UNKNOWN",
        "COMMAND_ARGS_INVALID",
        "COMMAND_TARGET_REQUIRED",
        "COMMAND_UNLINKED_RESTRICTED",
    }


def test_free_text_is_never_interpreted_even_when_it_looks_like_a_command() -> None:
    for text in ('task create "x" --criteria c', "colab task show task-1", "/colab-task show", ""):
        with pytest.raises(CommandError) as exc:
            parse_command(text)
        assert exc.value.code == "COMMAND_PREFIX_MISSING"


def test_mention_and_slash_forms_are_identical() -> None:
    a = parse_command('/colab task create "T" --criteria c')
    b = parse_command('@colab task create "T" --criteria c')
    assert (a.resource, a.verb, a.args) == (b.resource, b.verb, b.args)


def test_help_text_lists_every_verb_example() -> None:
    text = help_text()
    for spec in VERBS:
        assert spec.example in text
    assert help_text("link").count("\n") == 1
    assert help_text("nope") == "/colab help"


def test_error_carries_example_for_ephemeral_message() -> None:
    with pytest.raises(CommandError) as exc:
        parse_command("/colab task delegate task-1")
    assert exc.value.example == VERB_INDEX[("task", "delegate")].example
    assert "to" in exc.value.detail


# --- card / thread / callback schemas -------------------------------------------------------------


def test_card_and_callback_schemas_are_valid_and_cover_required_content() -> None:
    task_card = json.loads((MM_SCHEMAS / "task-card.v1.schema.json").read_text(encoding="utf-8"))
    bs_card = json.loads(
        (MM_SCHEMAS / "brainstorm-card.v1.schema.json").read_text(encoding="utf-8")
    )
    callback = json.loads(
        (MM_SCHEMAS / "action-callback.v1.schema.json").read_text(encoding="utf-8")
    )
    for schema in (task_card, bs_card, callback):
        Draft202012Validator.check_schema(schema)
    for key in (
        "title",
        "status",
        "assignee",
        "verification_status",
        "pending_approvals",
        "latest_progress",
        "artifact_links",
        "document_links",
        "subtasks",
        "buttons",
    ):
        assert key in task_card["properties"], key
    for key in ("participants", "remaining_turns", "budget", "status"):
        assert key in bs_card["properties"], key
    assert callback["x-validation-order"] == [
        "integration_token",
        "timestamp",
        "signature",
        "body_sha256",
        "nonce",
    ]
    assert set(callback["x-error-codes"]) == {
        "CALLBACK_SIGNATURE_INVALID",
        "CALLBACK_TIMESTAMP_EXPIRED",
        "CALLBACK_NONCE_REUSED",
        "CALLBACK_BODY_HASH_MISMATCH",
    }
    rules = json.loads((MM_SCHEMAS / "thread-rules.v1.json").read_text(encoding="utf-8"))["rules"]
    by_id = {r["id"]: r for r in rules}
    assert by_id["TR-05"]["value_s"] == 10 and by_id["TR-06"]["value_chars"] == 16000
    assert len(rules) >= 12


def test_task_card_example_validates() -> None:
    schema = json.loads((MM_SCHEMAS / "task-card.v1.schema.json").read_text(encoding="utf-8"))
    card = {
        "task_id": "task-1",
        "root_post_id": "p1",
        "title": "T",
        "status": "RUNNING",
        "risk": "LOW",
        "assignee": {"account_id": "a", "display_name": "Agent", "kind": "agent"},
        "verification_status": "NONE",
        "pending_approvals": [],
        "latest_progress": None,
        "artifact_links": [],
        "document_links": [],
        "buttons": [
            {
                "action": "submit",
                "label_key": "card.button.submit",
                "requires_permission": "task.submit",
            }
        ],
        "card_version": 3,
        "language": "en",
    }
    assert not list(Draft202012Validator(schema).iter_errors(card))
    card["status"] = "DONE"
    assert list(Draft202012Validator(schema).iter_errors(card))


# --- callback validation --------------------------------------------------------------------------


def _envelope(case: dict[str, Any], key: bytes, token: str) -> CallbackEnvelope:
    body = case["body"].encode()
    digest = body_digest(body)
    ts = int(case["timestamp"])
    signature = sign(key, ts, case["nonce"], digest)
    tamper = case["tamper"]
    if tamper == "token":
        token = "wrong-token"
    elif tamper == "signature":
        signature = "0" * 64
    elif tamper == "other_key":
        signature = sign(b"another-key", ts, case["nonce"], digest)
    elif tamper == "timestamp":
        ts += 1
    elif tamper == "body":
        body = b'{"a":2}'
    return CallbackEnvelope(token, ts, case["nonce"], digest, signature, body)


@pytest.mark.parametrize("case", CALLBACKS["cases"], ids=[c["name"] for c in CALLBACKS["cases"]])
def test_callback_validation(case: dict[str, Any]) -> None:
    key = CALLBACKS["signing_key"].encode()
    token = CALLBACKS["expected_token"]
    now = dt.datetime.fromisoformat(CALLBACKS["now"].replace("Z", "+00:00"))
    nonces = MemoryNonceStore()
    env = _envelope(case, key, token)
    kwargs: dict[str, Any] = {
        "expected_token": token,
        "signing_key": key,
        "nonces": nonces,
        "now": now,
    }
    if case["tamper"] == "replay":
        validate_callback(env, **kwargs)
    if case["tamper"] == "forged_then_valid":
        forged = CallbackEnvelope(
            token, env.timestamp, env.nonce, env.body_sha256, "0" * 64, env.body
        )
        with pytest.raises(CallbackError):
            validate_callback(forged, **kwargs)
    if case["expect"] == "ok":
        validate_callback(env, **kwargs)
    else:
        with pytest.raises(CallbackError) as exc:
            validate_callback(env, **kwargs)
        assert exc.value.code == case["expect"]


def test_commands_module_exposes_stable_prefixes() -> None:
    assert commands.COMMAND_PREFIXES == ("/colab", "@colab")
