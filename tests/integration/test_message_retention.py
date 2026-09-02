"""V-P2-29: retention 365→1 day, legal hold, virtual clock → expired Messages DEK-destroyed with
tombstones; zero deletions in held channels; provenance shows REDACTED_BY_RETENTION. Also the
canary-redaction invariant at the ingestion boundary (V-P2-10 storage side)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from server.application.messages import provenance_for
from server.channels.ingestion import (
    REDACTED_BY_RETENTION,
    ensure_conversation,
    ingest_message,
    load_message,
    normalize_for_document,
)
from server.channels.retention import (
    MESSAGE_TOMBSTONE_CHAIN,
    RetentionError,
    policy_for,
    retention_job,
    set_retention,
)
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.events.chain import verify_chain
from server.secrets.envelope import CryptoError, EnvelopeCrypto, MasterKey, new_master_key

pytestmark = pytest.mark.db

WS = uuid.uuid4()
ACTOR = uuid.uuid4()
CHAN_A = uuid.uuid4()
CHAN_B = uuid.uuid4()
T0 = dt.datetime(2026, 6, 1, 12, tzinfo=dt.UTC)
CANARY = "CANARY-NOT-A-SECRET-2029"


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as c:
        c.execute(
            text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, 'ws-ret', 'ret')"),
            {"i": WS},
        )
        c.execute(
            text(
                "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) "
                "VALUES (:i, 'acct-ret', :w, 'service', 'ret')"
            ),
            {"i": ACTOR, "w": WS},
        )
        for ch, cid in ((CHAN_A, "chan-ret-a"), (CHAN_B, "chan-ret-b")):
            c.execute(
                text(
                    "INSERT INTO channels "
                    "(id, channel_id, workspace_id, channel_type, display_name) "
                    "VALUES (:i, :c, :w, 'work', :c)"
                ),
                {"i": ch, "c": cid, "w": WS},
            )
    yield eng
    eng.dispose()


def _ingest(
    s: Session,
    crypto: EnvelopeCrypto,
    clock: FixedClock,
    channel: uuid.UUID,
    conv: str,
    n: int,
    body: str,
):  # type: ignore[no-untyped-def]
    ensure_conversation(
        s, workspace_id=str(WS), channel_id=str(channel), conversation_id=conv, mode="work"
    )
    return ingest_message(
        s,
        crypto,
        workspace_id=str(WS),
        channel_id=str(channel),
        conversation=conv,
        source="mattermost",
        source_message_id=f"post-{conv}-{n}",
        sender_label="@alice",
        sender_account_id=None,
        body=body,
        visibility="thread",
        clock=clock,
    )


def test_retention_job_shreds_expired_messages_and_respects_legal_hold(engine: Engine) -> None:
    crypto = EnvelopeCrypto(MasterKey.from_b64("mk-ret", new_master_key()))
    clock = FixedClock(T0)
    with Session(engine) as s, s.begin():
        assert policy_for(s, str(CHAN_A)).retention_days == 365  # default before any change
        set_retention(s, str(CHAN_A), 1, False, str(ACTOR), clock, correlation_id="corr-ret")
        set_retention(s, str(CHAN_B), 1, True, str(ACTOR), clock, correlation_id="corr-ret")
        with pytest.raises(RetentionError) as exc:
            set_retention(s, str(CHAN_A), 0, False, str(ACTOR), clock)
        assert exc.value.code == "RETENTION_DAYS_INVALID"
        ids_a = [
            _ingest(
                s, crypto, clock, CHAN_A, "conv-a", i, f"note {i} with secret={CANARY}"
            ).message_id
            for i in range(3)
        ]
        ids_b = [
            _ingest(s, crypto, clock, CHAN_B, "conv-b", i, f"held note {i}").message_id
            for i in range(2)
        ]
        dup = _ingest(s, crypto, clock, CHAN_A, "conv-a", 0, "again")
        assert dup.duplicate and dup.message_id == ids_a[0]
    # canary never persisted in plaintext anywhere; original readable only through the DEK
    with Session(engine) as s:
        for table, col in (
            ("messages", "body_redacted"),
            ("events", "payload::text"),
            ("audit_events", "redacted_metadata::text"),
        ):
            n = s.execute(
                text(f"SELECT count(*) FROM {table} WHERE {col} LIKE :c"),  # noqa: S608
                {"c": f"%{CANARY}%"},
            ).scalar_one()
            assert n == 0, table
        m0 = load_message(s, ids_a[0])
        assert m0 is not None and "<redacted:canary>" in m0.body_redacted and m0.body_key_ref
        row = s.execute(
            text("SELECT body_ciphertext FROM messages WHERE message_id = :m"), {"m": ids_a[0]}
        ).first()
        assert row is not None and CANARY.encode() not in bytes(row[0])
        assert crypto.decrypt(s, m0.body_key_ref, bytes(row[0]))["body"].endswith(CANARY)
    # nothing expires yet (1 day not elapsed)
    with Session(engine) as s, s.begin():
        early = retention_job(s, crypto, clock, workspace_id=str(WS), actor_account_id=str(ACTOR))
        assert early.destroyed == 0 and early.expired == 0
    clock.advance(dt.timedelta(days=2))
    with Session(engine) as s, s.begin():
        report = retention_job(s, crypto, clock, workspace_id=str(WS), actor_account_id=str(ACTOR))
    assert report.destroyed == 3 and report.expired == 5 and report.skipped_legal_hold == 2
    with Session(engine) as s:
        for mid in ids_a:
            m = load_message(s, mid)
            assert (
                m is not None
                and m.deleted_at is not None
                and m.body_redacted == REDACTED_BY_RETENTION
            )
            assert m.tombstone_ref
            status = s.execute(
                text("SELECT status, wrapped_dek FROM sensitive_keys WHERE key_ref = :k"),
                {"k": m.body_key_ref},
            ).first()
            assert status is not None and status[0] == "destroyed" and status[1] is None
            ct = s.execute(
                text("SELECT body_ciphertext FROM messages WHERE message_id = :m"), {"m": mid}
            ).scalar_one()
            with pytest.raises(CryptoError) as exc2:
                crypto.decrypt(s, str(m.body_key_ref), bytes(ct))
            assert exc2.value.code == "KEY_DESTROYED"
            assert normalize_for_document(m)["status"] == REDACTED_BY_RETENTION
        for mid in ids_b:
            m = load_message(s, mid)
            assert (
                m is not None and m.deleted_at is None and m.body_redacted.startswith("held note")
            )
        assert (
            s.execute(
                text("SELECT count(*) FROM messages WHERE channel_id = :c"), {"c": CHAN_A}
            ).scalar_one()
            == 3
        )  # rows kept
        assert (
            s.execute(
                text("SELECT count(*) FROM message_tombstones WHERE reason = 'RETENTION'")
            ).scalar_one()
            >= 3
        )
        assert verify_chain(s, MESSAGE_TOMBSTONE_CHAIN) == []
        assert (
            s.execute(
                text("SELECT count(*) FROM key_tombstones WHERE reason = 'RETENTION'")
            ).scalar_one()
            >= 3
        )
        prov = provenance_for(s, "conv-a")
        assert [p["status"] for p in prov] == [REDACTED_BY_RETENTION] * 3 and all(
            p["tombstone_ref"] for p in prov
        )
        assert [p["status"] for p in provenance_for(s, "conv-b")] == ["available"] * 2
    # tombstones are immutable
    with pytest.raises(DBAPIError, match="IMMUTABLE_ROW"), engine.begin() as c:
        c.execute(text("UPDATE message_tombstones SET reason = 'HARD_DELETE'"))
    # second run: idempotent
    with Session(engine) as s, s.begin():
        again = retention_job(s, crypto, clock, workspace_id=str(WS), actor_account_id=str(ACTOR))
    assert again.destroyed == 0 and again.expired == 2 and again.skipped_legal_hold == 2
    with Session(engine) as s:
        assert s.execute(text("SELECT count(*) FROM message_tombstones")).scalar_one() == 3
        audit = s.execute(
            text("SELECT count(*) FROM audit_events WHERE action = 'channel.retention_set'")
        ).scalar_one()
        assert audit == 2
