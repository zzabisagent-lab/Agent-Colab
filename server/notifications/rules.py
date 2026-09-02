"""Notification rules engine (development plan §7G, P1-13).

Pure planning (``plan_notifications``) is separated from persistence so the recipient set, dedupe,
mute/digest, and quiet-hour decisions are unit-testable. Persistence relies on the database's
UNIQUE ``dedupe_key`` so duplicates inside a dedupe window are impossible even under concurrency.
Notifications are never state authority: losing one has no effect on Task/Approval state.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from jsonschema import Draft202012Validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.clock import Clock, SystemClock
from server.notifications import outbox as ob
from server.notifications.selectors import channel_destinations, resolve_recipients

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULES = ROOT / "policy" / "notification-rules.yaml"
RULES_SCHEMA = ROOT / "schemas" / "api" / "notification" / "notification-rule.v1.schema.json"
PER_RECIPIENT_CHANNELS = ("mattermost:thread", "mattermost:dm", "work_item", "smtp")
CHANNEL_POST_CHANNELS = (
    "mattermost:approval_channel",
    "mattermost:ops_channel",
    "mattermost:channel",
)
DEFAULT_ENABLED_CHANNELS = frozenset(PER_RECIPIENT_CHANNELS + CHANNEL_POST_CHANNELS) - {"smtp"}

Event = dict[str, Any]


class NotificationRuleError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class QuietHours:
    start: dt.time
    end: dt.time
    timezone: str

    def deferral(self, now: dt.datetime) -> dt.datetime | None:
        """If ``now`` is inside the quiet window, return the window's end (UTC); else None."""
        local = now.astimezone(ZoneInfo(self.timezone))
        t = local.time()
        crosses_midnight = self.end <= self.start
        inside = (
            (self.start <= t < self.end)
            if not crosses_midnight
            else (t >= self.start or t < self.end)
        )
        if not inside:
            return None
        end_day = (
            local.date()
            if (not crosses_midnight or t < self.end)
            else local.date() + dt.timedelta(days=1)
        )
        end_local = dt.datetime.combine(end_day, self.end, tzinfo=ZoneInfo(self.timezone))
        return end_local.astimezone(dt.UTC)


@dataclass(frozen=True)
class Reminders:
    at_ratio: tuple[float, ...]
    at_expiry: bool


@dataclass(frozen=True)
class Rule:
    rule_id: str
    event_type: str
    recipient_selectors: tuple[str, ...]
    channels: tuple[str, ...]
    dedupe_window_seconds: int = 0
    quiet_hours: QuietHours | None = None
    reminders: Reminders | None = None
    re_notify_after_seconds: int | None = None
    enabled: bool = True
    version: int = 1


@dataclass(frozen=True)
class Preference:
    muted: bool = False
    digest: bool = False


@dataclass(frozen=True)
class PlannedNotification:
    rule_id: str
    recipient: str
    channels: tuple[str, ...]
    dedupe_key: str
    subject: str
    status: str  # queued | suppressed | digest
    deliver_at: dt.datetime
    digest_key: str | None = None


@dataclass
class NotificationRecord:
    notification_id: str | None
    rule_id: str
    recipient: str
    status: str  # queued | suppressed | digest | duplicate
    outbox_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------- rules loading
def _parse_rule(raw: dict[str, Any]) -> Rule:
    qh = raw.get("quiet_hours")
    quiet = None
    if qh:
        quiet = QuietHours(
            dt.time.fromisoformat(qh["start"]), dt.time.fromisoformat(qh["end"]), qh["timezone"]
        )
    rem = raw.get("reminders")
    reminders = (
        Reminders(tuple(float(x) for x in rem["at_ratio"]), bool(rem["at_expiry"])) if rem else None
    )
    return Rule(
        rule_id=raw["rule_id"],
        event_type=raw["event_type"],
        recipient_selectors=tuple(raw["recipient_selectors"]),
        channels=tuple(raw["channels"]),
        dedupe_window_seconds=int(raw.get("dedupe_window_seconds", 0)),
        quiet_hours=quiet,
        reminders=reminders,
        re_notify_after_seconds=raw.get("re_notify_after_seconds"),
        enabled=bool(raw.get("enabled", True)),
        version=int(raw.get("version", 1)),
    )


def load_rules(path: Path = DEFAULT_RULES) -> list[Rule]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    schema = json.loads(RULES_SCHEMA.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(raw), key=str)
    if errors:
        where = "/".join(str(p) for p in errors[0].path) or "<root>"
        raise NotificationRuleError("NOTIFICATION_RULES_INVALID", f"{where}: {errors[0].message}")
    rules = [_parse_rule(r) for r in raw["rules"]]
    ids = [r.rule_id for r in rules]
    if len(ids) != len(set(ids)):
        raise NotificationRuleError("NOTIFICATION_RULES_INVALID", "duplicate rule_id")
    return rules


def sync_rules(session: Session, workspace_id: str, rules: Iterable[Rule]) -> int:
    """Upsert the rule definitions into ``notification_rules`` (FK target of notifications)."""
    n = 0
    for r in rules:
        session.execute(
            text(
                "INSERT INTO notification_rules (rule_id, workspace_id, event_type, "
                "recipient_selector, channels, "
                "dedupe_window_seconds, quiet_hours, enabled, version) VALUES (:id, :ws, :et, "
                "CAST(:sel AS jsonb), "
                "CAST(:ch AS jsonb), :dw, CAST(:qh AS jsonb), :en, :v) ON CONFLICT (rule_id) DO "
                "UPDATE SET "
                "event_type = EXCLUDED.event_type, recipient_selector = "
                "EXCLUDED.recipient_selector, "
                "channels = EXCLUDED.channels, dedupe_window_seconds = "
                "EXCLUDED.dedupe_window_seconds, "
                "quiet_hours = EXCLUDED.quiet_hours, enabled = EXCLUDED.enabled, version = "
                "EXCLUDED.version"
            ),
            {
                "id": r.rule_id,
                "ws": uuid.UUID(workspace_id),
                "et": r.event_type,
                "sel": json.dumps(list(r.recipient_selectors)),
                "ch": json.dumps(list(r.channels)),
                "dw": r.dedupe_window_seconds,
                "qh": json.dumps(
                    {
                        "start": r.quiet_hours.start.isoformat("minutes"),
                        "end": r.quiet_hours.end.isoformat("minutes"),
                        "timezone": r.quiet_hours.timezone,
                    }
                )
                if r.quiet_hours
                else None,
                "en": r.enabled,
                "v": r.version,
            },
        )
        n += 1
    return n


# ---------------------------------------------------------------------------- pure planning
def subject_of(event: Event) -> str:
    return f"{event['aggregate_type']}:{event['aggregate_id']}"


def window_bucket(occurred_at: dt.datetime, window_seconds: int) -> int:
    if window_seconds <= 0:
        return int(occurred_at.timestamp() * 1000)  # no window: every Event is its own bucket
    return int(occurred_at.timestamp()) // window_seconds


def dedupe_key(rule_id: str, recipient: str, subject: str, bucket: int) -> str:
    return hashlib.sha256(f"{rule_id}|{recipient}|{subject}|{bucket}".encode()).hexdigest()


def occurred_at_of(event: Event) -> dt.datetime:
    value = event["occurred_at"]
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.UTC)
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(dt.UTC)


def next_hour(now: dt.datetime) -> dt.datetime:
    return (now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)).astimezone(
        dt.UTC
    )


def match_rules(rules: Iterable[Rule], event_type: str) -> list[Rule]:
    return [r for r in rules if r.enabled and r.event_type == event_type]


def plan_notifications(
    rules: Iterable[Rule],
    event: Event,
    recipients_by_selector: dict[str, list[str]],
    preferences: dict[str, Preference],
    now: dt.datetime,
    enabled_channels: frozenset[str] = DEFAULT_ENABLED_CHANNELS,
) -> list[PlannedNotification]:
    planned: list[PlannedNotification] = []
    seen: set[str] = set()
    subject = subject_of(event)
    occurred = occurred_at_of(event)
    for rule in match_rules(rules, str(event["type"])):
        recipients: set[str] = set()
        for selector in rule.recipient_selectors:
            recipients.update(recipients_by_selector.get(selector, []))
        channels = tuple(
            c for c in rule.channels if c in PER_RECIPIENT_CHANNELS and c in enabled_channels
        )
        bucket = window_bucket(occurred, rule.dedupe_window_seconds)
        for recipient in sorted(recipients):
            key = dedupe_key(rule.rule_id, recipient, subject, bucket)
            if key in seen:
                continue
            seen.add(key)
            pref = preferences.get(recipient, Preference())
            deliver_at = now
            status = "queued"
            digest_key: str | None = None
            if pref.muted or not channels:
                status = "suppressed"
            elif pref.digest:
                status = "digest"
                deliver_at = next_hour(now)
                digest_key = f"digest:{recipient}:{int(deliver_at.timestamp())}"
            elif rule.quiet_hours is not None:
                deferred = rule.quiet_hours.deferral(now)
                if deferred is not None:
                    deliver_at = deferred
            planned.append(
                PlannedNotification(
                    rule.rule_id, recipient, channels, key, subject, status, deliver_at, digest_key
                )
            )
    return planned


def reminder_times(
    valid_from: dt.datetime, expires_at: dt.datetime, reminders: Reminders
) -> list[tuple[str, dt.datetime]]:
    span = expires_at - valid_from
    out = [(f"reminder:{int(r * 100)}", valid_from + span * r) for r in reminders.at_ratio]
    if reminders.at_expiry:
        out.append(("reminder:expiry", expires_at))
    return out


# ---------------------------------------------------------------------------- engine
Resolver = Callable[[Session, str, Event, dt.datetime], list[str]]


class NotificationEngine:
    def __init__(
        self,
        rules: Iterable[Rule] | None = None,
        resolver: Resolver = resolve_recipients,
        clock: Clock | None = None,
        enabled_channels: frozenset[str] = DEFAULT_ENABLED_CHANNELS,
    ) -> None:
        self.rules = list(rules) if rules is not None else load_rules()
        self._resolve = resolver
        self._clock = clock or SystemClock()
        self._enabled = enabled_channels

    def load_preferences(
        self, session: Session, recipients: Iterable[str]
    ) -> dict[str, Preference]:
        ids = sorted(set(recipients))
        if not ids:
            return {}
        rows = session.execute(
            text(
                "SELECT account_id, muted, digest FROM notification_preferences WHERE account_id "
                "= ANY(:ids)"
            ),
            {"ids": [uuid.UUID(i) for i in ids]},
        ).all()
        return {str(r[0]): Preference(bool(r[1]), bool(r[2])) for r in rows}

    def on_event(self, session: Session, event: Event) -> list[NotificationRecord]:
        """Plan and persist notifications for one Event inside the caller's transaction."""
        now = self._clock.now()
        matching = match_rules(self.rules, str(event["type"]))
        if not matching:
            return []
        needed = sorted({s for r in matching for s in r.recipient_selectors})
        recipients_by_selector = {s: self._resolve(session, s, event, now) for s in needed}
        preferences = self.load_preferences(
            session, (rid for ids in recipients_by_selector.values() for rid in ids)
        )
        planned = plan_notifications(
            matching, event, recipients_by_selector, preferences, now, self._enabled
        )
        records = [self._persist(session, event, p) for p in planned]
        self._channel_posts(session, event, matching)
        self._followups(session, event, matching, records, now)
        return records

    def _persist(
        self, session: Session, event: Event, p: PlannedNotification
    ) -> NotificationRecord:
        notification_id = "ntf-" + uuid.uuid4().hex[:20]
        row = session.execute(
            text(
                "INSERT INTO notifications (notification_id, workspace_id, rule_id, "
                "source_event_id, "
                "recipient_account_id, channel, dedupe_key, status) VALUES (:id, :ws, :rule, :ev, "
                ":rcpt, :ch, "
                ":key, :status) ON CONFLICT (dedupe_key) DO NOTHING RETURNING notification_id"
            ),
            {
                "id": notification_id,
                "ws": uuid.UUID(str(event["workspace_id"])),
                "rule": p.rule_id,
                "ev": event["event_id"],
                "rcpt": uuid.UUID(p.recipient),
                "ch": ",".join(p.channels) or "none",
                "key": p.dedupe_key,
                "status": "queued" if p.status == "digest" else p.status,
            },
        ).first()
        if row is None:
            return NotificationRecord(None, p.rule_id, p.recipient, "duplicate")
        record = NotificationRecord(notification_id, p.rule_id, p.recipient, p.status)
        if p.status == "suppressed":
            return record
        base_payload = {
            "notification_id": notification_id,
            "rule_id": p.rule_id,
            "recipient_account_id": p.recipient,
            "event_id": event["event_id"],
            "event_type": event["type"],
            "subject": p.subject,
            "payload": event.get("payload", {}),
        }
        if p.status == "digest":
            digest_id = ob.enqueue_digest(
                session,
                str(event["workspace_id"]),
                p.recipient,
                p.digest_key or "",
                base_payload,
                event["event_id"],
                p.deliver_at,
            )
            record.outbox_ids.append(digest_id)
            return record
        for channel in p.channels:
            oid = ob.enqueue(
                session,
                str(event["workspace_id"]),
                "notification",
                f"{channel}:{p.recipient}",
                f"{notification_id}|{channel}",
                {**base_payload, "channel": channel},
                event["event_id"],
                p.deliver_at,
            )
            if oid:
                record.outbox_ids.append(oid)
        return record

    def _channel_posts(self, session: Session, event: Event, rules: list[Rule]) -> None:
        destinations = channel_destinations(session, event)
        for rule in rules:
            for channel in rule.channels:
                if channel not in CHANNEL_POST_CHANNELS or channel not in self._enabled:
                    continue
                for channel_uuid in destinations.get(channel, []):
                    ob.enqueue(
                        session,
                        str(event["workspace_id"]),
                        "notification_channel_post",
                        f"{channel}:{channel_uuid}",
                        f"{rule.rule_id}|{event['event_id']}|{channel}|{channel_uuid}",
                        {
                            "rule_id": rule.rule_id,
                            "event_id": event["event_id"],
                            "event_type": event["type"],
                            "channel": channel,
                            "payload": event.get("payload", {}),
                        },
                        event["event_id"],
                        self._clock.now(),
                    )

    def _followups(
        self,
        session: Session,
        event: Event,
        rules: list[Rule],
        records: list[NotificationRecord],
        now: dt.datetime,
    ) -> None:
        payload = event.get("payload", {})
        for rule in rules:
            queued = [
                r
                for r in records
                if r.rule_id == rule.rule_id and r.status == "queued" and r.notification_id
            ]
            if rule.reminders and payload.get("expires_at"):
                expires = dt.datetime.fromisoformat(
                    str(payload["expires_at"]).replace("Z", "+00:00")
                )
                for rec in queued:
                    assert rec.notification_id is not None
                    ob.schedule_reminders(
                        session,
                        str(event["workspace_id"]),
                        rec.notification_id,
                        rec.recipient,
                        [
                            c
                            for c in rule.channels
                            if c in PER_RECIPIENT_CHANNELS and c in self._enabled
                        ],
                        reminder_times(occurred_at_of(event), expires, rule.reminders),
                        event["event_id"],
                        {
                            "rule_id": rule.rule_id,
                            "event_type": event["type"],
                            "subject": subject_of(event),
                        },
                    )
            if rule.re_notify_after_seconds:
                for rec in queued:
                    assert rec.notification_id is not None
                    ob.schedule_reminders(
                        session,
                        str(event["workspace_id"]),
                        rec.notification_id,
                        rec.recipient,
                        [
                            c
                            for c in rule.channels
                            if c in PER_RECIPIENT_CHANNELS and c in self._enabled
                        ],
                        [("re_notify", now + dt.timedelta(seconds=rule.re_notify_after_seconds))],
                        event["event_id"],
                        {
                            "rule_id": rule.rule_id,
                            "event_type": event["type"],
                            "subject": subject_of(event),
                        },
                    )
