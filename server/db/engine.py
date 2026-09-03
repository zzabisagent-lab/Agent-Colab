"""SQLAlchemy engine/session factory and programmatic migrations."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[2]


def normalize_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


# A database outage must surface as a fast failure, not a hung request: readiness has to fail and
# writes have to answer 503 within 30 s (V-P7-06). Overridable for slow or loaded environments.
CONNECT_TIMEOUT_S = int(os.environ.get("AGENT_COLAB_DB_CONNECT_TIMEOUT_S", "5"))


#: Connection pool sizing. SQLAlchemy's defaults (5 + 10 overflow) sit well below the request
#: concurrency the ASGI server allows, so at the development plan §21.1 peak profile requests
#: queued on the pool and timed out instead of being served (found by the P7-04 load run).
#: The default here matches the sync-endpoint threadpool; both are configurable per deployment.
POOL_SIZE = int(os.environ.get("AGENT_COLAB_DB_POOL_SIZE", "20"))
POOL_MAX_OVERFLOW = int(os.environ.get("AGENT_COLAB_DB_MAX_OVERFLOW", "20"))


def make_engine(url: str) -> Engine:
    return create_engine(
        normalize_url(url),
        future=True,
        pool_pre_ping=True,
        pool_size=POOL_SIZE,
        max_overflow=POOL_MAX_OVERFLOW,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_S},
    )


RUNTIME_ROLE = "agent_colab_runtime"
ADMIN_ROLE = "agent_colab_admin"


def make_engine_for_role(url: str, role: str) -> Engine:
    """Engine whose connections run as the given application role (SET ROLE on connect).

    The login user owns the schema and runs migrations; the application never uses it directly.
    """
    engine = make_engine(url)

    @event.listens_for(engine, "connect")
    def _set_role(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET ROLE {role}")
        cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run_migrations(url: str, revision: str = "head") -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", normalize_url(url))
    command.upgrade(cfg, revision)
