"""Channel, template, and Mattermost provider REST (development plan §7.2 Channels)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from server.api.deps import current_principal
from server.api.dispatch import dispatch
from server.api.errors import ApiError
from server.application import channels as ch
from server.channels import templates as tpl
from server.db.engine import session_scope
from server.identity.principals import Principal

router = APIRouter(prefix="/api/v1", tags=["channels"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]


class ProviderBody(BaseModel):
    base_url: str
    team_name: str
    team_id: str
    bot_user_id: str | None = None
    identity_display: str | None = Field(default=None, pattern="^(override|prefix)$")


class SlashBody(BaseModel):
    provider_instance_id: str
    callback_url: str
    trigger: str = "colab"


class ImportBody(BaseModel):
    provider_instance_id: str
    external_channel_id: str
    channel_type: str = Field(default="work", pattern="^(work|brainstorm|approval|ops|custom)$")
    display_name: str | None = None
    template_id: str | None = None
    language: str | None = Field(default=None, pattern="^(ko|en)$")


class ConfigureBody(BaseModel):
    policy: dict[str, Any] | None = None
    documentation_template: str | None = None
    language: str | None = Field(default=None, pattern="^(ko|en)$")
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    legal_hold: bool | None = None
    template_id: str | None = None


class TemplateBody(BaseModel):
    template_id: str = Field(pattern="^[a-z][a-z0-9-]{1,63}$")
    name: str = Field(min_length=1, max_length=120)
    channel_type: str = Field(pattern="^(work|brainstorm|approval|ops|custom)$")
    definition: dict[str, Any]


class TemplateUpdateBody(BaseModel):
    definition: dict[str, Any]
    name: str | None = None


@router.post("/providers/mattermost/instances", status_code=201)
def register_instance(
    body: ProviderBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, ch.RegisterProviderInstance(**body.model_dump()))


@router.post("/providers/mattermost/commands/register", status_code=201)
def register_slash(body: SlashBody, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, ch.RegisterSlashCommand(**body.model_dump()))


@router.post("/channels/import", status_code=201)
def import_channel(body: ImportBody, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, ch.ImportChannel(**body.model_dump()))


@router.post("/channels/{channel_id}/configure")
def configure(
    channel_id: str, body: ConfigureBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request, principal, ch.ConfigureChannel(channel_id=channel_id, **body.model_dump())
    )


@router.post("/channels/{channel_id}/archive")
def archive(channel_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, ch.ArchiveChannel(channel_id=channel_id))


@router.get("/channels/{channel_id}")
def get_channel(channel_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        row = (
            session.execute(
                text(
                    "SELECT channel_id, channel_type, display_name, policy, "
                    "documentation_template, "
                    "language, retention_days, legal_hold, template_id, status, "
                    "external_channel_id "
                    "FROM channels WHERE channel_id = :c AND workspace_id = :ws"
                ),
                {"c": channel_id, "ws": runtime.resolve_workspace(session, principal.account_uuid)},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ApiError(404, "NOT_FOUND", "channel not found")
        return dict(row)


@router.get("/channels")
def list_channels(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        rows = (
            session.execute(
                text(
                    "SELECT channel_id, channel_type, display_name, status, template_id FROM "
                    "channels "
                    "WHERE workspace_id = :ws ORDER BY channel_id LIMIT 100"
                ),
                {"ws": runtime.resolve_workspace(session, principal.account_uuid)},
            )
            .mappings()
            .all()
        )
    return {"items": [dict(r) for r in rows]}


@router.get("/channel-templates")
def list_templates(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    runtime = request.app.state.runtime
    with session_scope(runtime.session_factory) as session:
        import uuid

        ws = uuid.UUID(runtime.resolve_workspace(session, principal.account_uuid))
        tpl.sync_defaults(session, ws)
        items = [t.__dict__ for t in tpl.list_templates(session, ws)]
    return {"items": items}


@router.post("/channel-templates", status_code=201)
def create_template(
    body: TemplateBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(request, principal, ch.CreateChannelTemplate(**body.model_dump()))


@router.put("/channel-templates/{template_id}")
def update_template(
    template_id: str, body: TemplateUpdateBody, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    return dispatch(
        request, principal, ch.UpdateChannelTemplate(template_id=template_id, **body.model_dump())
    )


@router.delete("/channel-templates/{template_id}")
def delete_template(template_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return dispatch(request, principal, ch.DeleteChannelTemplate(template_id=template_id))
