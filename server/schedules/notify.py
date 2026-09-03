"""Channel notices for Schedule Runs (development plan §10A.2 step 7, §7G; P5-07).

Start, result, failure, skip and late notices are posted in the Schedule's Mattermost channel
through the Renderer outbox as ``system_event`` posts, so a Telegram Bridge relays them only
when its content policy allows system events. Texts come from the i18n bundles in the channel's
language; ids and codes are never translated. One notice per Run and kind (``schedule_notices``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from server.channels.outbox import Delivery, enqueue_delivery
from server.channels.router import language_for_channel
from server.channels.task_cards import channel_target
from server.i18n import translate

if TYPE_CHECKING:
    from server.schedules.execution import ExecutionContext, RunLike, VersionLike

log = logging.getLogger(__name__)
KINDS = ("start", "result", "failure", "skip", "late", "backfill_warning")


def _post(
    ctx: ExecutionContext, run: RunLike, version: VersionLike, kind: str, **fields: Any
) -> str | None:
    target = channel_target(ctx.session, version.channel_id)
    if target is None:  # no Mattermost binding: the notice is recorded without a post
        _record(ctx, run, kind, None, version.channel_id)
        return None
    language = language_for_channel(
        ctx.session, target.provider_instance_uuid, target.external_channel_id
    )
    message = translate(
        f"schedule.notice.{kind}",
        language,
        schedule_id=run.schedule_id,
        run_id=run.run_id,
        task_id=run.task_id or "-",
        scheduled_for=run.scheduled_for.isoformat(),
        **fields,
    )
    dedupe_key = f"notice:{run.run_id}:{kind}"
    outbox_id = enqueue_delivery(
        ctx.session,
        workspace_id=ctx.workspace_id,
        source_event_id=run.result_event_id,
        delivery=Delivery(
            "mattermost.post",
            f"mattermost:{target.external_channel_id}",
            {
                "message": message,
                "props": {
                    "agent_colab": {
                        "subject_type": "schedule_run",
                        "subject_id": run.run_id,
                        "schedule_id": run.schedule_id,
                        "notice": kind,
                        "system_event": True,
                    },
                    "from_webhook": "true",
                },
            },
            dedupe_key,
            subject_type="schedule_run",
            subject_id=run.run_id,
            role="notice",
        ),
        provider_instance_id=target.provider_instance_id,
        external_channel_id=target.external_channel_id,
        now=ctx.now,
    )
    _record(ctx, run, kind, outbox_id, version.channel_id)
    return outbox_id


def _record(
    ctx: ExecutionContext, run: RunLike, kind: str, outbox_id: str | None, channel: str
) -> None:
    import uuid

    ctx.session.execute(
        text(
            "INSERT INTO schedule_notices (run_id, kind, dedupe_key, "
            "outbox_id, channel_id, created_at) "
            "VALUES (:r, :k, :d, :o, :c, :now) ON CONFLICT (run_id, kind) DO NOTHING"
        ),
        {
            "r": run.run_id,
            "k": kind,
            "d": f"notice:{run.run_id}:{kind}",
            "o": outbox_id,
            "c": uuid.UUID(channel),
            "now": ctx.now,
        },
    )


def start(ctx: ExecutionContext, run: RunLike, version: VersionLike) -> str | None:
    return _post(ctx, run, version, "start")


def result(ctx: ExecutionContext, run: RunLike, version: VersionLike) -> str | None:
    return _post(ctx, run, version, "result", status=run.status)


def failure(
    ctx: ExecutionContext, run: RunLike, version: VersionLike, code: str, detail: str
) -> str | None:
    return _post(
        ctx, run, version, "failure", status=run.status, error_code=code, detail=detail[:120]
    )


def skip(
    ctx: ExecutionContext, run: RunLike, version: VersionLike, code: str, detail: str
) -> str | None:
    return _post(ctx, run, version, "skip", error_code=code, detail=detail[:120])


def late(ctx: ExecutionContext, run: RunLike, version: VersionLike, delay_s: float) -> str | None:
    return _post(ctx, run, version, "late", delay_s=int(delay_s))


def notices(session: Any, run_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            # a virtual clock can stamp several notices in the same instant, so the lifecycle
            # order is the tiebreaker and the listing stays deterministic
            "SELECT kind, dedupe_key, outbox_id FROM schedule_notices "
            "WHERE run_id = :r ORDER BY created_at, "
            "CASE kind WHEN 'start' THEN 0 WHEN 'late' THEN 1 WHEN 'backfill_warning' THEN 2 "
            "WHEN 'skip' THEN 3 WHEN 'result' THEN 4 ELSE 5 END, kind"
        ),
        {"r": run_id},
    ).all()
    return [{"kind": str(r[0]), "dedupe_key": str(r[1]), "outbox_id": r[2]} for r in rows]
