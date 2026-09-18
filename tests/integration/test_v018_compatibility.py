from pathlib import Path
from typing import Any, cast

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.task_definition_executions import TaskDefinitionExecutionService
from backend.app.infrastructure.persistence.database import sqlite_database_url


def test_v017_historical_execution_without_v018_evidence_remains_readable(tmp_path: Path) -> None:
    database_path = tmp_path / "v017-history.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database_path)
    command.upgrade(config, "0026_ai_agent_v016")

    engine = create_engine(sqlite_database_url(database_path))
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO task_execution "
                "(id, task_definition_id, task_name, trigger, status, phase, "
                "source_execution_id, trace_id, config_snapshot, discovered_count, "
                "success_count, failed_count, skipped_count, started_at, finished_at, created_at) "
                "VALUES ('legacy-execution', NULL, 'legacy execution', 'MANUAL', "
                "'COMPLETED', 'COMPLETED', NULL, "
                "'00000000-0000-0000-0000-000000000017', '{}', 1, 1, 0, 0, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO task_execution_item "
                "(id, execution_id, unpack_task_id, source_object_key, name, source, "
                "size_bytes, phase, progress, result, error_code, error_summary_zh, "
                "technical_detail, retryable, retry_count, created_at, updated_at) "
                "VALUES ('legacy-item', 'legacy-execution', NULL, 'legacy-object', "
                "'Legacy.Movie.mkv', '/downloads/Legacy.Movie.mkv', 1024, 'COMPLETED', "
                "100, 'SUCCESS', NULL, NULL, NULL, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    command.upgrade(config, "head")
    command.check(config)
    factory: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False)
    service = TaskDefinitionExecutionService(
        factory,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        data_root=tmp_path,
    )

    execution = service.get_execution("legacy-execution")

    assert execution.status == "COMPLETED"
    assert execution.items[0].result == "SUCCESS"
    assert execution.items[0].execution_plan_id is None
    assert execution.items[0].risk_summary is None
    assert execution.items[0].approval is None
    assert execution.items[0].closure.status == "COMPLETE"
    assert execution.items[0].closure.filesystem_status == "NOT_APPLICABLE"
    assert execution.items[0].closure.downloader_status == "NOT_APPLICABLE"
    engine.dispose()
