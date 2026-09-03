from sqlalchemy import create_engine, text

import pytest

from tests.integration.setup_harness import Wizard, fresh_database


@pytest.fixture
def empty_db():
    yield from fresh_database()


def test_pre_db_rejection_is_not_migrated_to_audit(tmp_path, empty_db):
    wizard = Wizard(tmp_path)
    try:
        rejected = wizard.bootstrap("f" * 64)
        assert rejected.status_code == 403
        assert rejected.json()["code"] == "SETUP_TOKEN_INVALID"

        completed = wizard.run_to_locked(empty_db)
        assert completed["state"] == "LOCKED"

        engine = create_engine(empty_db)
        try:
            with engine.connect() as connection:
                count = connection.execute(
                    text(
                        "SELECT count(*) FROM audit_events "
                        "WHERE action = 'setup.token_rejected'"
                    )
                ).scalar_one()
        finally:
            engine.dispose()

        print(f"setup.token_rejected audit rows after bootstrap: {count}")
        assert count == 0
    finally:
        wizard.close()
