"""Message ingestion (P2-15; development plan §7H, §6.5 last bullet).

Ingestion scope: every post in Task and Brainstorm threads, messages relayed by Bridges, and the
whole channel only when the channel's documentation policy is ``full_channel``. The normalized
body is stored **after redaction**; the original body is kept only as envelope ciphertext under a
per-message DEK (destroyed by retention or hard delete). This module is the DLP boundary for
provider input (spec §15.7/21): secret values never reach ``messages.body_redacted``, Events,
audit rows, or documents.
"""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from server.domain.clock import Clock
from server.secrets.envelope import EnvelopeCrypto

REDACTED_BY_RETENTION = "REDACTED_BY_RETENTION"
REDACTED_BY_HARD_DELETE = "REDACTED_BY_HARD_DELETE"
DOCUMENTATION_POLICIES = ("task_threads", "full_channel")


class IngestionError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------- redaction (DLP boundary)
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("canary", re.compile(r"CANARY-NOT-A-SECRET-\d+")),
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("telegram_bot_token", re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{16,}=*")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}\b")),
)
_DSN = re.compile(r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)(?P<user>[^\s:/@]+):(?P<pw>[^\s@]+)@", re.I)
_ASSIGNMENT = re.compile(
    r"(?i)\b(?P<key>password|passwd|pwd|secret|api[_-]?key|access[_-]?key|token|private[_-]?key)"
    r"(?P<sep>\s*[:=]\s*)(?P<val>\"[^\"]+\"|'[^']+'|\S+)"
)
_HIGH_ENTROPY = re.compile(r"\b[A-Za-z0-9+/=_\-]{32,}\b")


def _shannon(value: str) -> float:
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    length = len(value)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def _looks_random(token: str) -> bool:
    if len(token) < 32:
        return False
    classes = sum(
        1 for pred in (str.islower, str.isupper, str.isdigit) if any(pred(ch) for ch in token)
    )
    return classes >= 3 and _shannon(token) >= 4.0


@dataclass(frozen=True)
class Redaction:
    text: str
    findings: tuple[str, ...]  # kinds only, never values

    @property
    def clean(self) -> bool:
        return not self.findings


class RedactionScanner:
    """Replace secret-looking spans with ``<redacted:kind>``; report kinds only."""

    def __init__(self, extra_patterns: Iterable[tuple[str, re.Pattern[str]]] = ()) -> None:
        self._patterns = (*_PATTERNS, *extra_patterns)

    def scan(self, body: str) -> Redaction:
        findings: list[str] = []
        out = body
        for kind, pattern in self._patterns:
            out, n = pattern.subn(f"<redacted:{kind}>", out)
            findings.extend([kind] * n)

        def _dsn(m: re.Match[str]) -> str:
            findings.append("dsn_password")
            return f"{m.group('scheme')}{m.group('user')}:<redacted:dsn_password>@"

        out = _DSN.sub(_dsn, out)

        def _assign(m: re.Match[str]) -> str:
            if m.group("val").startswith("<redacted:"):
                return m.group(0)
            findings.append("credential_assignment")
            return f"{m.group('key')}{m.group('sep')}<redacted:credential_assignment>"

        out = _ASSIGNMENT.sub(_assign, out)

        def _entropy(m: re.Match[str]) -> str:
            token = m.group(0)
            if token.startswith("<redacted:") or not _looks_random(token):
                return token
            findings.append("high_entropy")
            return "<redacted:high_entropy>"

        out = _HIGH_ENTROPY.sub(_entropy, out)
        return Redaction(out, tuple(findings))


DEFAULT_SCANNER = RedactionScanner()


# ---------------------------------------------------------------- scope rule
def in_ingestion_scope(
    *, documentation_policy: str, in_bound_thread: bool, bridge_relayed: bool
) -> bool:
    """§7H ingestion scope."""
    if documentation_policy not in DOCUMENTATION_POLICIES:
        raise IngestionError("DOCUMENTATION_POLICY_INVALID", documentation_policy)
    return in_bound_thread or bridge_relayed or documentation_policy == "full_channel"


# ---------------------------------------------------------------- conversations / messages
def message_id_for(source: str, source_message_id: str, conversation_id: str) -> str:
    digest = hashlib.sha256(f"{source}|{source_message_id}|{conversation_id}".encode()).hexdigest()
    return "msg-" + digest[:24]


def ensure_conversation(
    session: Session,
    *,
    workspace_id: str,
    channel_id: str,
    conversation_id: str,
    mode: str,
    source_thread: dict[str, Any] | None = None,
) -> str:
    """Create the Conversation row if absent (idempotent); returns conversation_id."""
    import json

    session.execute(
        text(
            "INSERT INTO conversations (id, conversation_id, workspace_id, channel_id, mode, "
            "source_thread) VALUES (:id, :cid, :ws, :ch, :mode, CAST(:st AS jsonb)) "
            "ON CONFLICT (conversation_id) DO NOTHING"
        ),
        {
            "id": uuid.uuid4(),
            "cid": conversation_id,
            "ws": uuid.UUID(workspace_id),
            "ch": uuid.UUID(channel_id),
            "mode": mode,
            "st": json.dumps(source_thread or {}),
        },
    )
    return conversation_id


@dataclass(frozen=True)
class IngestResult:
    message_id: str
    redaction_findings: tuple[str, ...]
    duplicate: bool
    encrypted: bool


@dataclass(frozen=True)
class StoredMessage:
    message_id: str
    conversation_id: str
    channel_id: str
    source: str
    source_message_id: str
    sender_label: str
    body_redacted: str
    visibility: str
    received_at: Any
    deleted_at: Any
    tombstone_ref: str | None
    body_key_ref: str | None = None
    redaction_findings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def status(self) -> str:
        if self.deleted_at is None:
            return "available"
        return self.body_redacted or REDACTED_BY_RETENTION


def ingest_message(
    session: Session,
    crypto: EnvelopeCrypto | None,
    *,
    workspace_id: str,
    channel_id: str,
    conversation: str,
    source: str,
    source_message_id: str,
    sender_label: str,
    sender_account_id: str | None,
    body: str,
    visibility: str,
    clock: Clock,
    retention_class: str = "default",
    scanner: RedactionScanner = DEFAULT_SCANNER,
    documentation_policy: str = "task_threads",
    in_bound_thread: bool = True,
    bridge_relayed: bool = False,
    received_at: Any | None = None,
) -> IngestResult:
    """Persist one provider message: redacted body always, original only as ciphertext."""
    if source not in ("mattermost", "telegram", "system"):
        raise IngestionError("MESSAGE_SOURCE_INVALID", source)
    if not in_ingestion_scope(
        documentation_policy=documentation_policy,
        in_bound_thread=in_bound_thread,
        bridge_relayed=bridge_relayed,
    ):
        raise IngestionError(
            "MESSAGE_OUT_OF_SCOPE", "not in a bound thread, relay, or full channel"
        )
    message_id = message_id_for(source, source_message_id, conversation)
    existing = session.execute(
        text(
            "SELECT message_id FROM messages WHERE source = :s AND source_message_id = :sm "
            "AND conversation_id = :c"
        ),
        {"s": source, "sm": source_message_id, "c": conversation},
    ).first()
    redaction = scanner.scan(body)
    if existing is not None:
        return IngestResult(str(existing[0]), redaction.findings, True, False)
    ciphertext: bytes | None = None
    key_ref: str | None = None
    if crypto is not None:
        ciphertext, key_ref = crypto.encrypt(
            session, workspace_id, "message", message_id, {"body": body}
        )
    session.execute(
        text(
            "INSERT INTO messages (id, message_id, workspace_id, conversation_id, channel_id, "
            "source, source_message_id, sender_account_id, sender_label, body_redacted, "
            "body_ciphertext, "
            "body_key_ref, visibility, received_at, retention_class) VALUES (:id, :mid, :ws, :cid, "
            ":ch, :src, :smid, :sender, :label, :body, :ct, :kr, :vis, :at, :rc)"
        ),
        {
            "id": uuid.uuid4(),
            "mid": message_id,
            "ws": uuid.UUID(workspace_id),
            "cid": conversation,
            "ch": uuid.UUID(channel_id),
            "src": source,
            "smid": source_message_id,
            "sender": uuid.UUID(sender_account_id) if sender_account_id else None,
            "label": sender_label,
            "body": redaction.text,
            "ct": ciphertext,
            "kr": key_ref,
            "vis": visibility,
            "at": received_at or clock.now(),
            "rc": retention_class,
        },
    )
    return IngestResult(message_id, redaction.findings, False, ciphertext is not None)


def load_message(session: Session, message_id: str) -> StoredMessage | None:
    row = (
        session.execute(
            text(
                "SELECT message_id, conversation_id, channel_id, source, source_message_id, "
                "sender_label, body_redacted, visibility, received_at, deleted_at, tombstone_ref, "
                "body_key_ref FROM messages WHERE message_id = :m"
            ),
            {"m": message_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return StoredMessage(
        message_id=str(row["message_id"]),
        conversation_id=str(row["conversation_id"]),
        channel_id=str(row["channel_id"]),
        source=str(row["source"]),
        source_message_id=str(row["source_message_id"]),
        sender_label=str(row["sender_label"]),
        body_redacted=str(row["body_redacted"]),
        visibility=str(row["visibility"]),
        received_at=row["received_at"],
        deleted_at=row["deleted_at"],
        tombstone_ref=row["tombstone_ref"],
        body_key_ref=row["body_key_ref"],
    )


def normalize_for_document(message: StoredMessage) -> dict[str, Any]:
    """Document-source view: redacted body or the retention/hard-delete marker, never plaintext."""
    return {
        "message_id": message.message_id,
        "conversation_id": message.conversation_id,
        "channel_id": message.channel_id,
        "source": message.source,
        "source_message_id": message.source_message_id,
        "sender_label": message.sender_label,
        "body_redacted": message.body_redacted if message.deleted_at is None else "",
        "visibility": message.visibility,
        "received_at": message.received_at.isoformat()
        if hasattr(message.received_at, "isoformat")
        else str(message.received_at),
        "status": message.status,
        "tombstone_ref": message.tombstone_ref,
    }
