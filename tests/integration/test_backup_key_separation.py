"""V-P4-10 / V-P4-17: a database dump (ciphertext + wrapped DEKs) cannot be decrypted with any
runtime credential material; only the master key, which never enters the database, can."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from server.application import secrets as sc
from server.db.engine import make_engine
from server.domain.clock import FixedClock
from server.secrets import local_provider as lp
from server.secrets.envelope import MasterKey, new_master_key
from tests.integration.secrets_seed import MASTER, T0, Seed

pytestmark = pytest.mark.db
SEED = Seed("bk")


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = make_engine(database_url)
    SEED.create(eng)
    yield eng
    eng.dispose()


def test_dump_plus_runtime_tokens_cannot_decrypt(engine: Engine) -> None:
    clock = FixedClock(T0)
    rt = SEED.runtime(engine, clock)
    value = b"backup-value-" + uuid.uuid4().hex.encode()
    with Session(engine) as s, s.begin():  # runtime credential material present in the dump
        s.execute(
            text(
                "INSERT INTO service_credentials (id, account_id, fingerprint, token_hash) "
                "VALUES (:i, :a, :f, :h)"
            ),
            {
                "i": uuid.uuid4(),
                "a": SEED.admin,
                "f": "sha256:bk",
                "h": hashlib.sha256(b"svc-bk-token").hexdigest(),
            },
        )
    ref = SEED.run(
        rt, SEED.admin_p, sc.RegisterSecret("bk/key", value, {"env": "prod"}), "reg"
    ).resource_id
    with Session(engine) as s:
        dump = s.execute(
            text(
                "SELECT dek_id, ciphertext, wrapped_dek, master_key_id FROM secret_versions "
                "WHERE secret_ref = :r"
            ),
            {"r": ref},
        ).first()
        assert dump is not None
        tokens = s.execute(text("SELECT token_hash FROM service_credentials")).all()
        whole = s.execute(
            text("SELECT string_agg(coalesce(metadata::text,''), ' ') FROM secrets")
        ).scalar()
    dek_id, ciphertext, wrapped, mk_id = str(dump[0]), bytes(dump[1]), bytes(dump[2]), str(dump[3])
    assert value not in ciphertext and value not in wrapped and value.decode() not in str(whole)
    assert mk_id == MASTER.key_id  # only the key *id* is stored, never the key
    # every runtime credential in the dump (service token hashes) fails to unwrap the DEK
    candidates = [bytes.fromhex(str(t[0]))[:32] for t in tokens if t[0]] + [
        hashlib.sha256(str(t[0]).encode()).digest() for t in tokens if t[0]
    ]
    assert candidates
    for key in candidates:
        with pytest.raises(InvalidTag):
            AESGCM(key).decrypt(wrapped[:12], wrapped[12:], dek_id.encode())
    with pytest.raises(InvalidTag):  # a different master key fails too
        lp._unwrap(MasterKey.from_b64("other", new_master_key()), wrapped, dek_id)
    dek = lp._unwrap(MASTER, wrapped, dek_id)  # the separated master key succeeds
    assert lp._decrypt(dek, ciphertext, dek_id) == value


def test_master_key_never_in_database(engine: Engine) -> None:
    with Session(engine) as s:
        tables = [
            r[0]
            for r in s.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
            ).all()
        ]
        import base64

        needle_b64 = base64.b64encode(MASTER.key).decode()
        for table in tables:
            cols = [
                r[0]
                for r in s.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns WHERE table_name = :t "
                        "AND data_type IN ('text','character varying','jsonb','json','bytea')"
                    ),
                    {"t": table},
                ).all()
            ]
            for col in cols:
                query = f'SELECT count(*) FROM "{table}" WHERE CAST("{col}" AS text) LIKE :b'  # noqa: S608
                hits = s.execute(text(query), {"b": f"%{needle_b64}%"}).scalar_one()
                assert int(hits) == 0, (table, col)
