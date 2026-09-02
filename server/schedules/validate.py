"""Schema validation for Schedule objects with stable error codes (P0-08, V-P0-11, V-P5-26)."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from server.schedules.contract import RunKind, ScheduleContractError, check_run_kind
from server.schedules.cron import CronError, load_zone
from server.schedules.cron import validate as validate_cron

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas" / "api" / "schedule"
FORBIDDEN_KEYS = frozenset(
    {"shell", "command", "script", "exec", "args", "cmd", "argv", "bash", "sh"}
)
SECRET_VALUE_KEYS = re.compile(r"(secret|token|password|passwd|api_key|apikey|private_key)$", re.I)
SHELL_META = re.compile(r"(\$\(|`|\|\||&&|[;|&><]|\n)")
_SHELL_WORDS = re.compile(
    r"^\s*(/bin/|/usr/bin/|bash\b|sh\b|cmd\.exe|powershell\b|python\s|sudo\b)", re.I
)


@lru_cache(maxsize=1)
def _validators() -> dict[str, Draft202012Validator]:
    resources = []
    schemas: dict[str, dict[str, Any]] = {}
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        schemas[path.name] = schema
        resources.append((schema["$id"], Resource.from_contents(schema)))
    registry = Registry().with_resources(resources)
    return {
        name: Draft202012Validator(schema, registry=registry) for name, schema in schemas.items()
    }


def _first_error(name: str, value: Any) -> str | None:
    errors = sorted(_validators()[name].iter_errors(value), key=lambda e: (list(e.path), e.message))
    if not errors:
        return None
    err = errors[0]
    path = "/".join(str(p) for p in err.path) or "<root>"
    return f"{path}: {err.message}"


def _scan_forbidden(value: Any, path: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN_KEYS:
                raise ScheduleContractError("ACTION_TEMPLATE_FORBIDDEN", f"{path}/{key}: shell key")
            if SECRET_VALUE_KEYS.search(key) and key not in {"secret_ref", "secret_refs"}:
                raise ScheduleContractError(
                    "ACTION_TEMPLATE_SECRET_VALUE", f"{path}/{key}: secret values are not allowed"
                )
            _scan_forbidden(item, f"{path}/{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _scan_forbidden(item, f"{path}[{i}]")
    elif isinstance(value, str) and (SHELL_META.search(value) or _SHELL_WORDS.search(value)):
        raise ScheduleContractError("ACTION_TEMPLATE_FORBIDDEN", f"{path}: shell string {value!r}")


def validate_action_template(template: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(template, dict):
        raise ScheduleContractError("ACTION_TEMPLATE_INVALID", "template must be an object")
    for key in template:
        if key in FORBIDDEN_KEYS:
            raise ScheduleContractError("ACTION_TEMPLATE_FORBIDDEN", f"top-level key {key!r}")
    problem = _first_error("action-template.v1.schema.json", template)
    if problem:
        raise ScheduleContractError("ACTION_TEMPLATE_INVALID", problem)
    _scan_forbidden(template.get("input", {}), "input")
    return template


def validate_agent_selection(selection: dict[str, Any]) -> dict[str, Any]:
    if isinstance(selection, dict) and any(
        k in selection for k in ("product", "vendor", "product_name", "model_vendor")
    ):
        raise ScheduleContractError(
            "AGENT_SELECTION_PRODUCT_FORBIDDEN", "product/vendor keys are not selection criteria"
        )
    problem = _first_error("agent-selection.v1.schema.json", selection)
    if problem:
        raise ScheduleContractError("AGENT_SELECTION_INVALID", problem)
    return selection


def validate_schedule_version(version: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(version, dict):
        raise ScheduleContractError("SCHEDULE_VERSION_INVALID", "version must be an object")
    if isinstance(version.get("action_template"), dict):
        validate_action_template(version["action_template"])
    if isinstance(version.get("agent_selection"), dict):
        validate_agent_selection(version["agent_selection"])
    problem = _first_error("schedule-version.v1.schema.json", version)
    if problem:
        raise ScheduleContractError("SCHEDULE_VERSION_INVALID", problem)
    try:
        load_zone(version["timezone"])
        validate_cron(version["cron_expression"], int(version.get("min_interval_minutes", 5)))
    except CronError as exc:
        raise ScheduleContractError(exc.code, exc.detail) from exc
    starts, ends = version.get("starts_at"), version.get("ends_at")
    if starts and ends and ends <= starts:
        raise ScheduleContractError("SCHEDULE_VERSION_INVALID", "ends_at must be after starts_at")
    return version


def validate_schedule_run(run: dict[str, Any]) -> dict[str, Any]:
    problem = _first_error("schedule-run.v1.schema.json", run)
    if problem:
        if "run_kind" in run and run["run_kind"] in RunKind.__members__:
            try:
                check_run_kind(
                    RunKind(run["run_kind"]), run.get("occurrence_key"), run.get("retry_of_run_id")
                )
            except ScheduleContractError:
                raise
        raise ScheduleContractError("RUN_INVALID", problem)
    check_run_kind(RunKind(run["run_kind"]), run.get("occurrence_key"), run.get("retry_of_run_id"))
    return run
