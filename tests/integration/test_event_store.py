"""P1-02: aggregate Event append — V-P1-01/02/03/04/06/21 and envelope crypto-shredding (V-P1-20)."""

from __future__ import annotations

import base64
import copy
import datetime as dt
import threading
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.events.hashing import compute_content_hash, verify_chain
from server.events.integrity import verify_events
from server.events.postgres_store import PostgresEventStore
from server.events.store import AppendRequest, EventStoreError
from server.secrets.envelope import CryptoError, EnvelopeCrypto, MasterKey, new_master_key

pytestmark = pytest.mark.db

WS = uuid.uuid4()
WS2 = uuid.uuid4()
ACTOR = uuid.uuid4()
ACTOR2 = uuid.uuid4()  # in WS2
CHANNEL = uuid.uuid4()
CLOCK = FixedClock(dt.datetime(2026, 2, 1, tzinfo=dt.UTC))
CRYPTO = EnvelopeCrypto(MasterKey.from_b64("mk-test", new_master_key()), CLOCK)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    with eng.begin() as c:
        for ws, name in ((WS, "ws-es"), (WS2, "ws-es-2")):
            c.execute(
                text("INSERT INTO workspaces (id, workspace_id, name) VALUES (:i, :w, :w)"),
                {"i": ws, "w": name},
            )
        for acc, ws, name in ((ACTOR, WS, "acct-es"), (ACTOR2, WS2, "acct-es-2")):
            c.execute(
                text(
                    "INSERT INTO accounts (id, account_id, workspace_id, account_type, display_name) VALUES (:i, :a, :w, 'service', :a)"
                ),
                {"i": acc, "a": name, "w": ws},
            )
        c.execute(
            text(
                "INSERT INTO channels (id, channel_id, workspace_id, channel_type, display_name) VALUES (:i, 'chan-es', :w, 'work', 'es')"
            ),
            {"i": CHANNEL, "w": WS},
        )
    yield eng
    eng.dispose()


def _req(agg: str, seq_key: str, **over: object) -> AppendRequest:
    base: dict[str, object] = {
        "workspace_id": str(WS),
        "aggregate_type": "task",
        "aggregate_id": agg,
        "type": "TASK_PROGRESS_REPORTED",
        "actor_account_id": str(ACTOR),
        "correlation_id": "corr-es",
        "idempotency_scope": "task:progress",
        "idempotency_key": seq_key,
        "payload": {"task_id": agg, "summary": f"step {seq_key}"},
        "task_id": agg,
        "channel_id": str(CHANNEL),
    }
    base.update(over)
    return AppendRequest(**base)  # type: ignore[arg-type]


def _store(session: Session) -> PostgresEventStore:
    return PostgresEventStore(session, crypto=CRYPTO, clock=CLOCK)


def test_append_sequence_and_response_consistent(engine: Engine) -> None:  # V-P1-01
    with Session(engine) as s, s.begin():
        st = _store(s)
        created = st.append(
            _req(
                "task-es-1",
                "k1",
                type="TASK_CREATED",
                idempotency_scope="task:create",
                payload={
                    "task_id": "task-es-1",
                    "root_task_id": "task-es-1",
                    "channel_id": str(CHANNEL),
                    "title": "t",
                    "domain": "research",
                    "risk": "LOW",
                },
            )
        )
        second = st.append(_req("task-es-1", "k2", caused_by=created.event_id))
        assert (created.aggregate_seq, second.aggregate_seq) == (1, 2)
        events = st.stream(str(WS), "task", "task-es-1")
        assert [e["aggregate_seq"] for e in events] == [1, 2]
        assert events[1]["previous_hash"] == created.content_hash == events[0]["content_hash"]
        assert events[1]["caused_by"] == created.event_id
        assert verify_chain(events) == []
        assert second.recorded_seq > created.recorded_seq


def test_idempotent_retry_returns_same_event_and_conflict_on_different_body(
    engine: Engine,
) -> None:  # V-P1-02/03
    with Session(engine) as s, s.begin():
        st = _store(s)
        first = st.append(_req("task-es-2", "same"))
        again = st.append(_req("task-es-2", "same"))
        assert (
            again.replayed
            and again.event_id == first.event_id
            and again.aggregate_seq == first.aggregate_seq
        )
        with pytest.raises(EventStoreError) as exc:
            st.append(
                _req("task-es-2", "same", payload={"task_id": "task-es-2", "summary": "different"})
            )
        assert exc.value.code == "IDEMPOTENCY_CONFLICT"
        # a different scope with the same key is independent
        other = st.append(
            _req(
                "task-es-2",
                "same",
                idempotency_scope="task:accept",
                type="TASK_ACCEPTED",
                payload={"task_id": "task-es-2", "assignee_account_id": str(ACTOR)},
            )
        )
        assert not other.replayed and other.aggregate_seq == 2
        assert len(st.stream(str(WS), "task", "task-es-2")) == 2


def test_concurrent_identical_retries_produce_one_event(
    engine: Engine,
) -> None:  # V-P1-02 concurrent
    results: list[object] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        with Session(engine) as s, s.begin():
            barrier.wait()
            try:
                results.append(_store(s).append(_req("task-es-3", "race")))
            except EventStoreError as exc:  # pragma: no cover - would be a defect
                results.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = {getattr(r, "event_id", None) for r in results}
    assert len(ids) == 1 and None not in ids, results
    with Session(engine) as s:
        assert len(_store(s).stream(str(WS), "task", "task-es-3")) == 1


def test_concurrent_appends_keep_unique_monotonic_sequence(engine: Engine) -> None:  # V-P1-04
    n = 12
    barrier = threading.Barrier(n)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        with Session(engine) as s, s.begin():
            barrier.wait()
            try:
                _store(s).append(_req("task-es-4", f"c{i}"))
            except Exception as exc:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with Session(engine) as s:
        events = _store(s).stream(str(WS), "task", "task-es-4")
    assert [e["aggregate_seq"] for e in events] == list(range(1, n + 1))
    assert verify_chain(events) == []
    # concurrency on other aggregates (approval, agent) uses the same path
    with Session(engine) as s, s.begin():
        st = _store(s)
        a = st.append(
            _req(
                "apr-es-1",
                "a1",
                aggregate_type="approval",
                type="APPROVAL_REQUESTED",
                idempotency_scope="approval:request",
                payload={
                    "approval_id": "apr-es-1",
                    "subject_type": "task",
                    "subject_id": "task-es-4",
                    "action": "x",
                    "risk": "LOW",
                    "expires_at": "2026-02-02T00:00:00.000Z",
                },
                task_id=None,
            )
        )
        g = st.append(
            _req(
                "agent-es-1",
                "g1",
                aggregate_type="agent",
                type="AGENT_REGISTERED",
                idempotency_scope="agent:register",
                payload={
                    "agent_id": "agent-es-1",
                    "account_id": str(ACTOR),
                    "adapter_type": "mcp",
                    "display_name": "x",
                },
                task_id=None,
                channel_id=None,
            )
        )
        assert a.aggregate_seq == 1 and g.aggregate_seq == 1


def test_expected_sequence_cas(engine: Engine) -> None:
    with Session(engine) as s, s.begin():
        st = _store(s)
        st.append(_req("task-es-5", "e1", expected_seq=1))
        with pytest.raises(EventStoreError) as exc:
            st.append(_req("task-es-5", "e2", expected_seq=1))
        assert exc.value.code == "SEQUENCE_CONFLICT"
        assert st.append(_req("task-es-5", "e3", expected_seq=2)).aggregate_seq == 2


def test_causality_and_workspace_errors_have_no_side_effects(engine: Engine) -> None:  # V-P1-06
    cases = [
        (_req("task-es-6", "x1", caused_by="evt-" + "f" * 32), "CAUSED_BY_UNKNOWN"),
        (_req("task-es-6", "x2", actor_account_id=str(ACTOR2)), "WORKSPACE_MISMATCH"),
        (_req("task-es-6", "x3", channel_id=str(uuid.uuid4())), "CHANNEL_UNKNOWN"),
        (_req("task-es-6", "x4", type="NOT_A_TYPE"), "UNKNOWN_EVENT_TYPE"),
        (_req("task-es-6", "x5", payload={"summary": "missing task_id"}), "PAYLOAD_INVALID"),
        (_req("task-es-6", "x6", aggregate_type="approval"), "AGGREGATE_TYPE_MISMATCH"),
    ]
    for request, code in cases:
        with Session(engine) as s, s.begin():
            with pytest.raises(EventStoreError) as exc:
                _store(s).append(request)
            assert exc.value.code == code, request
    with Session(engine) as s:
        assert _store(s).stream(str(WS), "task", "task-es-6") == []
    # an event cannot cause itself or one from another workspace
    with Session(engine) as s, s.begin():
        st = _store(s)
        base = st.append(_req("task-es-6", "ok1"))
        with Session(engine) as s2, s2.begin():
            other = PostgresEventStore(s2, crypto=CRYPTO, clock=CLOCK).append(
                _req(
                    "task-es-6b",
                    "w2",
                    workspace_id=str(WS2),
                    actor_account_id=str(ACTOR2),
                    channel_id=None,
                    task_id="task-es-6b",
                )
            )
        with pytest.raises(EventStoreError) as exc:
            st.append(_req("task-es-6", "ok2", caused_by=other.event_id))
        assert exc.value.code == "WORKSPACE_MISMATCH"
        assert st.append(_req("task-es-6", "ok3", caused_by=base.event_id)).aggregate_seq == 2


def test_hash_chain_recompute_and_tamper_detection(engine: Engine) -> None:  # V-P1-21
    with Session(engine) as s, s.begin():
        st = _store(s)
        for i in range(3):
            st.append(
                _req("task-es-7", f"h{i}", sensitive={"note": f"secret {i}"} if i == 1 else None)
            )
        events = st.stream(str(WS), "task", "task-es-7")
    assert verify_chain(events) == []
    for ev in events:
        assert compute_content_hash(ev) == ev["content_hash"]
    # tamper each hashed input in memory: 100% detected
    detected = 0
    for field, value in (
        ("payload", {"task_id": "task-es-7", "summary": "forged"}),
        ("sensitive_payload_ciphertext", base64.b64encode(b"\x00" * 40).decode()),
        ("previous_hash", "0" * 64),
        ("type", "TASK_STARTED"),
        ("actor_account_id", str(ACTOR2)),
    ):
        tampered = copy.deepcopy(events)
        tampered[1][field] = value
        if tampered[1]["aggregate_seq"] == 1 and field == "previous_hash":
            continue
        detected += bool(verify_chain(tampered))
    assert detected == 5
    # tamper in the database bypassing the trigger (superuser only) -> integrity job detects it
    with engine.begin() as c:
        c.execute(text("ALTER TABLE events DISABLE TRIGGER trg_events_immutable"))
        c.execute(
            text(
                "UPDATE events SET payload = payload || '{\"summary\": \"forged\"}' WHERE aggregate_id = 'task-es-7' AND aggregate_seq = 2"
            )
        )
        c.execute(text("ALTER TABLE events ENABLE TRIGGER trg_events_immutable"))
    with Session(engine) as s:
        problems = verify_events(s, str(WS))
    assert any("task-es-7" in p and "content_hash mismatch" in p for p in problems)
    with engine.begin() as c:
        c.execute(text("ALTER TABLE events DISABLE TRIGGER trg_events_immutable"))
        c.execute(
            text(
                "UPDATE events SET payload = payload || '{\"summary\": \"step h1\"}' WHERE aggregate_id = 'task-es-7' AND aggregate_seq = 2"
            )
        )
        c.execute(text("ALTER TABLE events ENABLE TRIGGER trg_events_immutable"))
    with Session(engine) as s:
        assert not [p for p in verify_events(s, str(WS)) if "task-es-7" in p]


def test_crypto_shredding_leaves_bytes_and_hash_unchanged(engine: Engine) -> None:  # V-P1-20 core
    with Session(engine) as s, s.begin():
        st = _store(s)
        res = st.append(
            _req("task-es-8", "s1", sensitive={"credential_hint": "CANARY-NOT-A-SECRET-0042"})
        )
        ev = st.get(res.event_id)
        assert (
            ev is not None
            and ev["sensitive_payload_key_ref"]
            and ev["payload"] == {"task_id": "task-es-8", "summary": "step s1"}
        )
        assert "CANARY" not in ev["sensitive_payload_ciphertext"]
        assert CRYPTO.decrypt(
            s, ev["sensitive_payload_key_ref"], base64.b64decode(ev["sensitive_payload_ciphertext"])
        ) == {"credential_hint": "CANARY-NOT-A-SECRET-0042"}
        before = s.execute(
            text(
                "SELECT content_hash, sensitive_payload_ciphertext, payload FROM events WHERE event_id = :e"
            ),
            {"e": res.event_id},
        ).first()
        CRYPTO.destroy(s, ev["sensitive_payload_key_ref"], str(ACTOR), "hard delete test")
        with pytest.raises(CryptoError) as exc:
            CRYPTO.decrypt(
                s,
                ev["sensitive_payload_key_ref"],
                base64.b64decode(ev["sensitive_payload_ciphertext"]),
            )
        assert exc.value.code == "KEY_DESTROYED"
        after = s.execute(
            text(
                "SELECT content_hash, sensitive_payload_ciphertext, payload FROM events WHERE event_id = :e"
            ),
            {"e": res.event_id},
        ).first()
        assert before is not None and after is not None and tuple(before) == tuple(after)
        assert s.execute(
            text("SELECT status, wrapped_dek FROM sensitive_keys WHERE key_ref = :k"),
            {"k": ev["sensitive_payload_key_ref"]},
        ).first() == ("destroyed", None)
        assert (
            s.execute(
                text("SELECT count(*) FROM key_tombstones WHERE key_ref = :k"),
                {"k": ev["sensitive_payload_key_ref"]},
            ).scalar()
            == 1
        )
        with pytest.raises(
            EventStoreError
        ) as exc2:  # new sensitive appends on the destroyed target are impossible
            st.append(_req("task-es-8", "s2", sensitive={"x": 1}))
        assert exc2.value.code in ("KEY_DESTROYED",)
