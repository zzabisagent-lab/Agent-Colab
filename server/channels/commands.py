"""`/colab` command grammar contract (development plan §7A.2, spec §8.7; P0-10).

`/colab <resource> <verb> [positional...] [--key value ...]` — the same grammar is accepted after an
`@colab` mention. Free text without either prefix is never interpreted as a command
(``COMMAND_PREFIX_MISSING``). Every resource/verb pair has a JSON Schema in
``schemas/api/commands/<resource>.<verb>.v1.schema.json`` describing the parsed argument object;
``parse_command`` tokenizes, maps positionals/options to named arguments, validates against the
schema, applies the thread-context target rule and the unlinked-user restriction, and either
returns a ``ParsedCommand`` or raises ``CommandError`` with a stable code and a correct example.

Error messages are i18n-neutral keys (``message_key``) resolved by the i18n layer (P2-16).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas" / "api" / "commands"
SCHEMA_ID_BASE = "https://agent-colab.dev/schemas/api/commands"
COMMAND_PREFIXES = ("/colab", "@colab")
READ = "task.read"  # read verbs share the task-scoped read permission (§6.9 vocabulary)

_ID_PATTERNS: dict[str, str] = {
    "task_id": r"^task-[A-Za-z0-9._-]{1,120}$",
    "approval_id": r"^apr-[A-Za-z0-9._-]{1,120}$",
    "brainstorm_id": r"^bs-[A-Za-z0-9._-]{1,120}$",
    "decision_id": r"^dec-[A-Za-z0-9._-]{1,120}$",
    "schedule_id": r"^sch-[A-Za-z0-9._-]{1,120}$",
    "run_id": r"^run-[A-Za-z0-9._-]{1,120}$",
    "subject_id": r"^(task|bs)-[A-Za-z0-9._-]{1,120}$",
    "schedule_or_run_id": r"^(sch|run)-[A-Za-z0-9._-]{1,120}$",
}
_MENTION = r"^@[A-Za-z0-9._-]{1,64}$"
_PRINCIPAL = r"^(@[A-Za-z0-9._-]{1,64}|agent-[A-Za-z0-9._-]{1,120})$"


@dataclass(frozen=True)
class Field:
    """One argument of a verb: where it comes from and how it is typed."""

    name: str
    kind: str  # id | text | enum | mention | principal | mentions | int | ref | code | bool
    required: bool = False
    positional: bool = False
    repeatable: bool = False
    enum: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    id_type: str | None = None  # key into _ID_PATTERNS
    description: str = ""


@dataclass(frozen=True)
class VerbSpec:
    resource: str
    verb: str
    permission: str  # exact permission or `<resource>.*` (any permission with that prefix)
    fields: tuple[Field, ...] = ()
    target: str | None = None  # kind resolved from the thread when the id positional is omitted
    target_field: str | None = None
    unlinked_allowed: bool = False
    example: str = ""
    description: str = ""

    @property
    def schema_name(self) -> str:
        return f"{self.resource}.{self.verb}.v1.schema.json"


def _id(name: str, id_type: str, required: bool = False, desc: str = "") -> Field:
    return Field(name, "id", required=required, positional=True, id_type=id_type, description=desc)


def _text(name: str, required: bool = True, desc: str = "") -> Field:
    return Field(name, "text", required=required, positional=True, description=desc)


def _opt(name: str, kind: str = "text", **kw: Any) -> Field:
    return Field(name, kind, **kw)


REASON_CODES = ("CAPABILITY_UNSUPPORTED", "CAPACITY", "POLICY", "OTHER")
RISKS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
CONTRIBUTIONS = ("IDEA", "CHALLENGE", "QUESTION", "GUIDANCE")

_TASK_TARGET = _id("task_id", "task_id", desc="omitted inside a Task thread")
_BS_TARGET = _id("brainstorm_id", "brainstorm_id", desc="omitted inside a Brainstorm thread")

VERBS: tuple[VerbSpec, ...] = (
    # task
    VerbSpec(
        "task",
        "create",
        "task.create",
        (
            _text("title", desc="quoted title"),
            _opt(
                "criteria",
                "text",
                required=True,
                repeatable=True,
                description="§7D.1 acceptance criteria, one or more",
            ),
            _opt("domain"),
            _opt("risk", "enum", enum=RISKS),
            _opt("assignee", "principal"),
            _opt("parent", "id", id_type="task_id"),
        ),
        example=(
            '/colab task create "Write the report" --criteria "report.md attached" '
            "--domain research"
        ),
        description="Create a Task with acceptance criteria in the current channel",
    ),
    VerbSpec(
        "task",
        "delegate",
        "task.delegate",
        (_TASK_TARGET, _opt("to", "principal", required=True), _opt("reason")),
        target="task",
        target_field="task_id",
        example="/colab task delegate task-123 --to @research-agent",
    ),
    VerbSpec(
        "task",
        "accept",
        "task.accept",
        (_TASK_TARGET,),
        target="task",
        target_field="task_id",
        example="/colab task accept task-123",
    ),
    VerbSpec(
        "task",
        "reject",
        "task.accept",
        (_TASK_TARGET, _opt("reason", "enum", required=True, enum=REASON_CODES)),
        target="task",
        target_field="task_id",
        example="/colab task reject task-123 --reason CAPACITY",
    ),
    VerbSpec(
        "task",
        "progress",
        "task.progress",
        (_TASK_TARGET, _text("message", desc="progress note")),
        target="task",
        target_field="task_id",
        example='/colab task progress task-123 "half of the sections drafted"',
    ),
    VerbSpec(
        "task",
        "submit",
        "task.submit",
        (
            _TASK_TARGET,
            _opt(
                "evidence",
                "ref",
                required=True,
                repeatable=True,
                description="one evidence ref per criterion",
            ),
        ),
        target="task",
        target_field="task_id",
        example="/colab task submit task-123 --evidence art-45 --evidence art-46",
    ),
    VerbSpec(
        "task",
        "complete",
        "task.complete",
        (_TASK_TARGET,),
        target="task",
        target_field="task_id",
        example="/colab task complete task-123",
    ),
    VerbSpec(
        "task",
        "cancel",
        "task.cancel",
        (_TASK_TARGET, _opt("reason")),
        target="task",
        target_field="task_id",
        example='/colab task cancel task-123 --reason "no longer needed"',
    ),
    VerbSpec(
        "task",
        "reassign",
        "task.reassign",
        (_TASK_TARGET, _opt("to", "principal", required=True), _opt("reason")),
        target="task",
        target_field="task_id",
        example="/colab task reassign task-123 --to @other-agent --reason offline",
    ),
    VerbSpec(
        "task",
        "show",
        READ,
        (_TASK_TARGET,),
        target="task",
        target_field="task_id",
        example="/colab task show task-123",
    ),
    VerbSpec(
        "task",
        "list",
        "task.list",
        (
            _opt("status"),
            _opt("assignee", "principal"),
            _opt("limit", "int", minimum=1, maximum=100),
        ),
        example="/colab task list --status RUNNING --limit 20",
    ),
    # approve
    VerbSpec(
        "approve",
        "request",
        "approval.request",
        (
            _TASK_TARGET,
            _opt("action", required=True),
            _opt("scope"),
            _opt("risk", "enum", enum=RISKS),
        ),
        target="task",
        target_field="task_id",
        example=(
            "/colab approve request task-123 --action external_send --scope mail:ops@example.test"
        ),
    ),
    VerbSpec(
        "approve",
        "grant",
        "approval.decide",
        (_id("approval_id", "approval_id", required=True), _opt("comment")),
        example="/colab approve grant apr-77",
    ),
    VerbSpec(
        "approve",
        "reject",
        "approval.decide",
        (_id("approval_id", "approval_id", required=True), _opt("reason")),
        example='/colab approve reject apr-77 --reason "scope too wide"',
    ),
    VerbSpec(
        "approve",
        "show",
        "approval.read",
        (_id("approval_id", "approval_id", required=True),),
        example="/colab approve show apr-77",
    ),
    VerbSpec(
        "approve",
        "list",
        "approval.read",
        (_opt("status"), _opt("limit", "int", minimum=1, maximum=100)),
        example="/colab approve list --status PENDING",
    ),
    # verify
    VerbSpec(
        "verify",
        "assign",
        "verification.assign",
        (_TASK_TARGET, _opt("to", "principal")),
        target="task",
        target_field="task_id",
        example="/colab verify assign task-123 --to @reviewer",
    ),
    VerbSpec(
        "verify",
        "pass",
        "verification.submit",
        (_TASK_TARGET, _opt("evidence", "ref", required=True, repeatable=True), _opt("comment")),
        target="task",
        target_field="task_id",
        example="/colab verify pass task-123 --evidence art-90",
    ),
    VerbSpec(
        "verify",
        "fail",
        "verification.submit",
        (
            _TASK_TARGET,
            _opt("evidence", "ref", required=True, repeatable=True),
            _opt("finding", "text", repeatable=True),
        ),
        target="task",
        target_field="task_id",
        example='/colab verify fail task-123 --evidence art-91 --finding "criterion 2 not met"',
    ),
    VerbSpec(
        "verify",
        "block",
        "verification.submit",
        (_TASK_TARGET, _opt("reason", required=True)),
        target="task",
        target_field="task_id",
        example='/colab verify block task-123 --reason "sandbox unreachable"',
    ),
    VerbSpec(
        "verify",
        "show",
        "verification.read",
        (_TASK_TARGET,),
        target="task",
        target_field="task_id",
        example="/colab verify show task-123",
    ),
    # brainstorm
    VerbSpec(
        "brainstorm",
        "start",
        "brainstorm.facilitate",
        (
            _text("topic"),
            _opt("participants", "mentions", required=True),
            _opt("turns-per-agent", "int", minimum=1, maximum=100),
            _opt("max-consecutive", "int", minimum=1, maximum=10),
            _opt("total-turns", "int", minimum=1, maximum=1000),
            _opt("budget", "int", minimum=0),
            _opt("time", "int", minimum=1),
        ),
        example='/colab brainstorm start "Q4 roadmap" --participants @a,@b --turns-per-agent 5',
    ),
    VerbSpec(
        "brainstorm",
        "contribute",
        "brainstorm.contribute",
        (_BS_TARGET, _text("text"), _opt("type", "enum", enum=CONTRIBUTIONS)),
        target="brainstorm",
        target_field="brainstorm_id",
        example='/colab brainstorm contribute bs-5 "ship the beta first" --type IDEA',
    ),
    VerbSpec(
        "brainstorm",
        "summarize",
        "brainstorm.summarize",
        (_BS_TARGET,),
        target="brainstorm",
        target_field="brainstorm_id",
        example="/colab brainstorm summarize bs-5",
    ),
    VerbSpec(
        "brainstorm",
        "decide",
        "brainstorm.facilitate",
        (
            _BS_TARGET,
            _text("statement"),
            _opt("rationale", required=True),
            _opt("source", "ref", required=True, repeatable=True, description="source event ids"),
            _opt("vote", "bool"),
        ),
        target="brainstorm",
        target_field="brainstorm_id",
        example=(
            '/colab brainstorm decide bs-5 "adopt option B" --rationale "lower cost" '
            "--source evt-1 --source evt-2"
        ),
    ),
    VerbSpec(
        "brainstorm",
        "taskify",
        "brainstorm.facilitate",
        (_BS_TARGET, _opt("decision", "id", id_type="decision_id")),
        target="brainstorm",
        target_field="brainstorm_id",
        example="/colab brainstorm taskify bs-5 --decision dec-9",
    ),
    VerbSpec(
        "brainstorm",
        "pause",
        "brainstorm.facilitate",
        (_BS_TARGET,),
        target="brainstorm",
        target_field="brainstorm_id",
        example="/colab brainstorm pause bs-5",
    ),
    VerbSpec(
        "brainstorm",
        "resume",
        "brainstorm.facilitate",
        (
            _BS_TARGET,
            _opt("turns-per-agent", "int", minimum=1, maximum=100),
            _opt("max-consecutive", "int", minimum=1, maximum=10),
            _opt("total-turns", "int", minimum=1, maximum=1000),
            _opt("budget", "int", minimum=0),
            _opt("time", "int", minimum=1),
        ),
        target="brainstorm",
        target_field="brainstorm_id",
        example="/colab brainstorm resume bs-5 --total-turns 60",
    ),
    VerbSpec(
        "brainstorm",
        "close",
        "brainstorm.facilitate",
        (_BS_TARGET,),
        target="brainstorm",
        target_field="brainstorm_id",
        example="/colab brainstorm close bs-5",
    ),
    VerbSpec(
        "brainstorm",
        "show",
        "brainstorm.read",
        (_BS_TARGET,),
        target="brainstorm",
        target_field="brainstorm_id",
        example="/colab brainstorm show bs-5",
    ),
    # doc
    VerbSpec(
        "doc",
        "show",
        "document.read",
        (_id("subject_id", "subject_id"),),
        target="subject",
        target_field="subject_id",
        example="/colab doc show task-123",
    ),
    VerbSpec(
        "doc",
        "review",
        "document.review",
        (
            _id("subject_id", "subject_id"),
            _opt("result", "enum", required=True, enum=("approve", "reject")),
            _opt("comment"),
        ),
        target="subject",
        target_field="subject_id",
        example="/colab doc review task-123 --result approve",
    ),
    VerbSpec(
        "doc",
        "publish",
        "document.publish",
        (_id("subject_id", "subject_id"), _opt("publisher")),
        target="subject",
        target_field="subject_id",
        example="/colab doc publish task-123 --publisher git",
    ),
    # schedule
    VerbSpec(
        "schedule",
        "show",
        "schedule.read",
        (_id("schedule_or_run_id", "schedule_or_run_id", required=True),),
        example="/colab schedule show sch-3",
    ),
    VerbSpec(
        "schedule",
        "list",
        "schedule.read",
        (_opt("status"), _opt("limit", "int", minimum=1, maximum=100)),
        example="/colab schedule list --status ENABLED",
    ),
    VerbSpec(
        "schedule",
        "run-now",
        "schedule.run",
        (_id("schedule_id", "schedule_id", required=True),),
        example="/colab schedule run-now sch-3",
    ),
    VerbSpec(
        "schedule",
        "pause",
        "schedule.manage",
        (_id("schedule_id", "schedule_id", required=True),),
        example="/colab schedule pause sch-3",
    ),
    VerbSpec(
        "schedule",
        "resume",
        "schedule.manage",
        (_id("schedule_id", "schedule_id", required=True),),
        example="/colab schedule resume sch-3",
    ),
    VerbSpec(
        "schedule",
        "cancel-run",
        "schedule.run",
        (_id("run_id", "run_id", required=True), _opt("reason")),
        example="/colab schedule cancel-run run-31",
    ),
    # link / notify / help (self-service)
    VerbSpec(
        "link", "start", "identity.link", (), unlinked_allowed=True, example="/colab link start"
    ),
    VerbSpec(
        "link",
        "confirm",
        "identity.link",
        (Field("code", "code", required=True, positional=True),),
        unlinked_allowed=True,
        example="/colab link confirm 123456",
    ),
    VerbSpec(
        "notify",
        "mute",
        "notification.self",
        (_opt("until"),),
        example="/colab notify mute --until 2026-01-16T09:00:00Z",
    ),
    VerbSpec("notify", "unmute", "notification.self", (), example="/colab notify unmute"),
    VerbSpec(
        "notify",
        "digest",
        "notification.self",
        (_opt("interval", "enum", enum=("hourly", "off")),),
        example="/colab notify digest --interval hourly",
    ),
    VerbSpec(
        "help",
        "",
        "none",
        (
            Field(
                "resource",
                "enum",
                positional=True,
                enum=(
                    "task",
                    "approve",
                    "verify",
                    "brainstorm",
                    "doc",
                    "schedule",
                    "link",
                    "notify",
                ),
            ),
        ),
        unlinked_allowed=True,
        example="/colab help task",
    ),
)

VERB_INDEX: dict[tuple[str, str], VerbSpec] = {(v.resource, v.verb): v for v in VERBS}
RESOURCES: tuple[str, ...] = tuple(dict.fromkeys(v.resource for v in VERBS))


class CommandError(ValueError):
    """Stable, side-effect-free command rejection rendered as an ephemeral message."""

    def __init__(self, code: str, message_key: str, example: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail or message_key}")
        self.code = code
        self.message_key = message_key
        self.example = example
        self.detail = detail


@dataclass(frozen=True)
class CommandContext:
    """Where the command was typed and who typed it."""

    linked: bool = True  # the Mattermost user has an active ExternalIdentityLink
    thread_subject_kind: str | None = None  # task | brainstorm
    thread_subject_id: str | None = None


@dataclass(frozen=True)
class ParsedCommand:
    resource: str
    verb: str
    permission: str
    args: dict[str, Any]
    target_kind: str | None = None
    target_id: str | None = None
    target_source: str | None = None  # explicit | thread
    raw: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)


# --- tokenizer -------------------------------------------------------------------------------

_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|(\S+)')


def tokenize(text: str) -> list[str]:
    """Split on whitespace, honouring double/single quotes and backslash escapes inside quotes."""
    tokens: list[str] = []
    pos = 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            pos += 1
            continue
        if m.group(1) is not None:
            tokens.append(re.sub(r"\\(.)", r"\1", m.group(1)))
        elif m.group(2) is not None:
            tokens.append(re.sub(r"\\(.)", r"\1", m.group(2)))
        else:
            tokens.append(m.group(3))
        pos = m.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return tokens


def strip_prefix(text: str) -> list[str] | None:
    """Return tokens after `/colab` or `@colab`, or None when the text is not a command."""
    stripped = text.strip()
    for prefix in COMMAND_PREFIXES:
        if (
            stripped == prefix
            or stripped.startswith(prefix + " ")
            or stripped.startswith(prefix + "\n")
        ):
            return tokenize(stripped[len(prefix) :])
    return None


def split_options(tokens: list[str]) -> tuple[list[str], dict[str, list[str | bool]]]:
    """Separate positionals from `--key value`, `--key=value`, and bare `--flag` options."""
    positionals: list[str] = []
    options: dict[str, list[str | bool]] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--") and len(tok) > 2:
            key, eq, value = tok[2:].partition("=")
            if eq:
                options.setdefault(key, []).append(value)
            elif i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                options.setdefault(key, []).append(tokens[i + 1])
                i += 1
            else:
                options.setdefault(key, []).append(True)
        else:
            positionals.append(tok)
        i += 1
    return positionals, options


# --- schema -----------------------------------------------------------------------------------


def _field_schema(f: Field) -> dict[str, Any]:
    base: dict[str, Any]
    if f.kind == "id":
        base = {"type": "string", "pattern": _ID_PATTERNS[f.id_type or f.name]}
    elif f.kind == "enum":
        base = {"type": "string", "enum": list(f.enum)}
    elif f.kind == "mention":
        base = {"type": "string", "pattern": _MENTION}
    elif f.kind == "principal":
        base = {"type": "string", "pattern": _PRINCIPAL}
    elif f.kind == "mentions":
        base = {
            "type": "array",
            "items": {"type": "string", "pattern": _MENTION},
            "minItems": 1,
            "uniqueItems": True,
        }
    elif f.kind == "int":
        base = {"type": "integer"}
        if f.minimum is not None:
            base["minimum"] = f.minimum
        if f.maximum is not None:
            base["maximum"] = f.maximum
    elif f.kind == "ref":
        base = {"type": "string", "pattern": r"^[A-Za-z0-9._:/#@+-]{1,300}$"}
    elif f.kind == "code":
        base = {"type": "string", "pattern": r"^[0-9]{6}$"}
    elif f.kind == "bool":
        base = {"type": "boolean"}
    else:
        base = {"type": "string", "minLength": 1, "maxLength": 16000}
    if f.description:
        base["description"] = f.description
    if f.repeatable:
        return {"type": "array", "items": base, "minItems": 1, "maxItems": 50}
    return base


def build_schema(spec: VerbSpec) -> dict[str, Any]:
    """JSON Schema (Draft 2020-12) of the parsed argument object for one resource/verb."""
    props = {f.name: _field_schema(f) for f in spec.fields}
    required = [f.name for f in spec.fields if f.required]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{SCHEMA_ID_BASE}/{spec.schema_name}",
        "title": f"/colab {spec.resource} {spec.verb}".rstrip(),
        "description": spec.description
        or f"Arguments of `/colab {spec.resource} {spec.verb}`.".replace("  ", " "),
        "x-permission": spec.permission,
        "x-target": spec.target,
        "x-target-field": spec.target_field,
        "x-unlinked-allowed": spec.unlinked_allowed,
        "x-example": spec.example,
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


@cache
def _validator(schema_name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS_DIR / schema_name).read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


# --- parsing ----------------------------------------------------------------------------------


def _coerce(f: Field, raw: str | bool) -> Any:
    if f.kind == "bool":
        return True if raw is True else str(raw).lower() in ("true", "1", "yes")
    if raw is True:
        return True  # bare flag for a non-bool field → schema rejects with a stable error
    if f.kind == "int":
        try:
            return int(raw)
        except ValueError:
            return raw
    if f.kind == "mentions":
        return [m for m in re.split(r"[,\s]+", str(raw)) if m]
    return raw


def bind_arguments(
    spec: VerbSpec, positionals: list[str], options: dict[str, list[str | bool]]
) -> dict[str, Any]:
    """Map positionals and options to the named argument object (no validation yet)."""
    args: dict[str, Any] = {}
    pos = list(positionals)
    positional_fields = [f for f in spec.fields if f.positional]
    # an optional leading id positional is only consumed when the token looks like that id
    for f in positional_fields:
        if not pos:
            break
        if f.kind == "id" and not f.required:
            pattern = _ID_PATTERNS[f.id_type or f.name]
            if re.match(pattern, pos[0]):
                args[f.name] = pos.pop(0)
            continue
        args[f.name] = _coerce(f, pos.pop(0))
    if pos:
        args["__extra_positionals__"] = pos
    option_fields = {f.name: f for f in spec.fields if not f.positional}
    for key, values in options.items():
        opt = option_fields.get(key)
        if opt is None:
            args[key] = values[-1] if len(values) == 1 else values
            continue
        coerced = [_coerce(opt, v) for v in values]
        if opt.repeatable:
            flat: list[Any] = []
            for c in coerced:
                flat.extend(c if isinstance(c, list) else [c])
            args[opt.name] = flat
        else:
            args[opt.name] = coerced[-1] if len(coerced) == 1 else coerced
    return args


def parse_command(text: str, context: CommandContext | None = None) -> ParsedCommand:
    """Tokenize, resolve, validate, and apply thread/link rules. Raises ``CommandError``."""
    context = context or CommandContext()
    tokens = strip_prefix(text)
    if tokens is None:
        raise CommandError("COMMAND_PREFIX_MISSING", "command.prefix_missing", "/colab help")
    if not tokens:
        return ParsedCommand("help", "", "none", {}, raw=text)
    resource = tokens[0].lower()
    if resource not in RESOURCES:
        raise CommandError(
            "COMMAND_RESOURCE_UNKNOWN", "command.resource_unknown", "/colab help", resource
        )
    if resource == "help":
        verb, rest = "", tokens[1:]
    else:
        if len(tokens) < 2:
            raise CommandError(
                "COMMAND_VERB_UNKNOWN", "command.verb_missing", f"/colab help {resource}", resource
            )
        verb, rest = tokens[1].lower(), tokens[2:]
    spec = VERB_INDEX.get((resource, verb))
    if spec is None:
        raise CommandError(
            "COMMAND_VERB_UNKNOWN",
            "command.verb_unknown",
            f"/colab help {resource}",
            f"{resource} {verb}",
        )
    if not context.linked and not spec.unlinked_allowed:
        raise CommandError(
            "COMMAND_UNLINKED_RESTRICTED",
            "command.unlinked_restricted",
            "/colab link start",
            f"{resource} {verb}",
        )
    positionals, options = split_options(rest)
    args = bind_arguments(spec, positionals, options)
    if "__extra_positionals__" in args:
        raise CommandError(
            "COMMAND_ARGS_INVALID",
            "command.args_extra",
            spec.example,
            " ".join(args["__extra_positionals__"]),
        )
    target_kind, target_id, target_source = None, None, None
    if spec.target and spec.target_field:
        if spec.target_field in args:
            target_kind, target_id, target_source = spec.target, args[spec.target_field], "explicit"
        elif context.thread_subject_id and _thread_matches(
            spec.target, context.thread_subject_kind
        ):
            args[spec.target_field] = context.thread_subject_id
            target_kind, target_id, target_source = spec.target, context.thread_subject_id, "thread"
        else:
            raise CommandError(
                "COMMAND_TARGET_REQUIRED",
                "command.target_required",
                spec.example,
                spec.target_field,
            )
        if spec.target == "subject":
            target_kind = "task" if str(target_id).startswith("task-") else "brainstorm"
    errors = sorted(
        _validator(spec.schema_name).iter_errors(args), key=lambda e: (list(e.path), e.message)
    )
    if errors:
        first = errors[0]
        path = "/".join(str(p) for p in first.path) or "<args>"
        raise CommandError(
            "COMMAND_ARGS_INVALID", "command.args_invalid", spec.example, f"{path}: {first.message}"
        )
    return ParsedCommand(
        resource, verb, spec.permission, args, target_kind, target_id, target_source, raw=text
    )


def _thread_matches(target: str, thread_kind: str | None) -> bool:
    if thread_kind is None:
        return False
    if target == "subject":
        return thread_kind in ("task", "brainstorm")
    return target == thread_kind


def help_text(resource: str | None = None) -> str:
    """Usage text (English reference wording; localized copies are keyed by `help.<resource>`)."""
    lines: list[str] = []
    for spec in VERBS:
        if resource and spec.resource != resource:
            continue
        lines.append(spec.example)
    if not lines:
        return "/colab help"
    return "\n".join(lines)
