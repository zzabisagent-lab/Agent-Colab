"""Channel templates (spec §8.1): four protected defaults plus administrator-defined templates.

Definitions are validated against ``schemas/api/channel/channel-template.v1.schema.json``. The
defaults come from ``policy/channel-templates.yaml`` and are synced into ``channel_templates``
(protected rows); user templates are CRUD rows in the same table.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from sqlalchemy import text
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_PATH = ROOT / "policy" / "channel-templates.yaml"
SCHEMA_PATH = ROOT / "schemas" / "api" / "channel" / "channel-template.v1.schema.json"
DEFAULT_TEMPLATE_IDS = ("work", "brainstorm", "approval", "ops")


class TemplateError(ValueError):
    def __init__(self, code: str, detail: str = "", status: int = 409) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = status


@dataclass(frozen=True)
class ChannelTemplate:
    template_id: str
    name: str
    channel_type: str
    definition: dict[str, Any]
    protected: bool
    version: int


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def validate_definition(definition: dict[str, Any]) -> None:
    errors = sorted(_validator().iter_errors(definition), key=lambda e: (list(e.path), e.message))
    if errors:
        first = errors[0]
        path = "/".join(str(p) for p in first.path) or "<root>"
        raise TemplateError("TEMPLATE_INVALID", f"{path}: {first.message}", 422)


@lru_cache(maxsize=1)
def default_templates() -> dict[str, ChannelTemplate]:
    doc = yaml.safe_load(TEMPLATES_PATH.read_text(encoding="utf-8"))
    out: dict[str, ChannelTemplate] = {}
    for tid, spec in doc["templates"].items():
        validate_definition(spec["definition"])
        out[tid] = ChannelTemplate(
            tid, spec["name"], spec["channel_type"], dict(spec["definition"]), True, 1
        )
    if tuple(out) != DEFAULT_TEMPLATE_IDS:
        raise TemplateError("TEMPLATE_DEFAULTS_INVALID", str(tuple(out)), 500)
    return out


def sync_defaults(session: Session, workspace_uuid: uuid.UUID) -> None:
    """Insert the protected defaults for a Workspace (idempotent; never overwrites)."""
    for t in default_templates().values():
        session.execute(
            text(
                "INSERT INTO channel_templates (template_id, workspace_id, name, channel_type, "
                "definition, protected, version) VALUES (:id, :ws, :name, :type, CAST(:d AS "
                "jsonb), "
                "true, 1) ON CONFLICT (workspace_id, template_id) DO NOTHING"
            ),
            {
                "id": t.template_id,
                "ws": workspace_uuid,
                "name": t.name,
                "type": t.channel_type,
                "d": json.dumps(t.definition),
            },
        )


def _row(r: Any) -> ChannelTemplate:
    return ChannelTemplate(
        str(r["template_id"]),
        str(r["name"]),
        str(r["channel_type"]),
        dict(r["definition"]),
        bool(r["protected"]),
        int(r["version"]),
    )


def get_template(
    session: Session, workspace_uuid: uuid.UUID, template_id: str
) -> ChannelTemplate | None:
    row = (
        session.execute(
            text(
                "SELECT template_id, name, channel_type, definition, protected, version "
                "FROM channel_templates WHERE workspace_id = :ws AND template_id = :t "
                "AND status = 'active'"
            ),
            {"ws": workspace_uuid, "t": template_id},
        )
        .mappings()
        .first()
    )
    return _row(row) if row else None


def list_templates(session: Session, workspace_uuid: uuid.UUID) -> list[ChannelTemplate]:
    rows = session.execute(
        text(
            "SELECT template_id, name, channel_type, definition, protected, version "
            "FROM channel_templates WHERE workspace_id = :ws AND status = 'active' "
            "ORDER BY protected DESC, template_id"
        ),
        {"ws": workspace_uuid},
    ).mappings()
    return [_row(r) for r in rows]


def create_template(
    session: Session,
    workspace_uuid: uuid.UUID,
    template_id: str,
    name: str,
    channel_type: str,
    definition: dict[str, Any],
    created_by: uuid.UUID,
) -> ChannelTemplate:
    if (
        template_id in DEFAULT_TEMPLATE_IDS
        or get_template(session, workspace_uuid, template_id) is not None
    ):
        raise TemplateError("TEMPLATE_EXISTS", template_id)
    validate_definition(definition)
    session.execute(
        text(
            "INSERT INTO channel_templates (template_id, workspace_id, name, channel_type, "
            "definition, protected, version, created_by) VALUES (:id, :ws, :name, :type, "
            "CAST(:d AS jsonb), false, 1, :by)"
        ),
        {
            "id": template_id,
            "ws": workspace_uuid,
            "name": name,
            "type": channel_type,
            "d": json.dumps(definition),
            "by": created_by,
        },
    )
    return ChannelTemplate(template_id, name, channel_type, definition, False, 1)


def update_template(
    session: Session,
    workspace_uuid: uuid.UUID,
    template_id: str,
    definition: dict[str, Any],
    name: str | None = None,
) -> ChannelTemplate:
    current = get_template(session, workspace_uuid, template_id)
    if current is None:
        raise TemplateError("TEMPLATE_NOT_FOUND", template_id, 404)
    validate_definition(definition)
    session.execute(
        text(
            "UPDATE channel_templates SET definition = CAST(:d AS jsonb), name = :name, "
            "version = version + 1, updated_at = now() "
            "WHERE workspace_id = :ws AND template_id = :id"
        ),
        {
            "d": json.dumps(definition),
            "name": name or current.name,
            "id": template_id,
            "ws": workspace_uuid,
        },
    )
    return ChannelTemplate(
        template_id,
        name or current.name,
        current.channel_type,
        definition,
        current.protected,
        current.version + 1,
    )


def delete_template(session: Session, workspace_uuid: uuid.UUID, template_id: str) -> None:
    current = get_template(session, workspace_uuid, template_id)
    if current is None:
        raise TemplateError("TEMPLATE_NOT_FOUND", template_id, 404)
    if current.protected:
        raise TemplateError("TEMPLATE_PROTECTED", template_id)
    session.execute(
        text(
            "UPDATE channel_templates SET status = 'deleted', updated_at = now() WHERE template_id "
            "= :id"
        ),
        {"id": template_id},
    )
