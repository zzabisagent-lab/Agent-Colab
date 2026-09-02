"""Create an isolated migrated DB, invoke the documented projection CLI, then remove it."""

from __future__ import annotations

import os
import subprocess

from sqlalchemy import create_engine, text

from server.db.engine import normalize_url, run_migrations


MAINTENANCE_URL = normalize_url(os.environ["AGENT_COLAB_TEST_DATABASE_URL"])
DATABASE_NAME = "colab_verify_cli"
DATABASE_URL = MAINTENANCE_URL.rsplit("/", 1)[0] + f"/{DATABASE_NAME}"


def drop_database(engine) -> None:  # type: ignore[no-untyped-def]
    with engine.connect() as connection:
        connection.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()"
            ),
            {"name": DATABASE_NAME},
        )
        connection.execute(text(f'DROP DATABASE IF EXISTS "{DATABASE_NAME}"'))


maintenance = create_engine(MAINTENANCE_URL, isolation_level="AUTOCOMMIT")
try:
    drop_database(maintenance)
    with maintenance.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{DATABASE_NAME}"'))
    run_migrations(DATABASE_URL)
    environment = dict(os.environ, AGENT_COLAB_DATABASE_URL=DATABASE_URL)
    completed = subprocess.run(  # noqa: S603
        ["uv", "run", "python", "-m", "server.projections.runner", "rebuild", "tasks"],
        env=environment,
        check=False,
        text=True,
        capture_output=True,
    )
    print(f"command_exit_code={completed.returncode}")
    print(completed.stdout.strip())
    if completed.stderr:
        print(completed.stderr.strip())
    completed.check_returncode()
finally:
    drop_database(maintenance)
    maintenance.dispose()
