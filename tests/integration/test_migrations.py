from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from backend.app.infrastructure.persistence.database import sqlite_database_url


def test_alembic_upgrade_creates_m1_core_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "migration-test.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database_path)

    command.upgrade(config, "head")
    command.check(config)

    engine = create_engine(sqlite_database_url(database_path))
    inspector = inspect(engine)
    assert {"unpack_task", "task_event", "operation_journal"}.issubset(
        set(inspector.get_table_names())
    )
    task_unique_names = {
        constraint["name"] for constraint in inspector.get_unique_constraints("unpack_task")
    }
    assert task_unique_names == {"uq_unpack_task_idempotency_key"}
    assert {
        constraint["name"] for constraint in inspector.get_unique_constraints("operation_journal")
    } == {"uq_operation_journal_idempotency_key"}
    engine.dispose()
