from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from backend.app.infrastructure.persistence.database import sqlite_database_url


def test_unpack_v2_foundation_migration_creates_new_model(tmp_path: Path) -> None:
    database = tmp_path / "unpack-v2.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database)

    command.upgrade(config, "0035_unpack_v2_foundation")

    engine = create_engine(sqlite_database_url(database))
    try:
        inspector = inspect(engine)
        expected = {
            "unpack_definition",
            "unpack_definition_selected_source",
            "unpack_execution",
            "unpack_execution_item",
            "unpack_match_candidate",
            "unpack_review_decision",
        }
        assert expected.issubset(set(inspector.get_table_names()))

        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("0035_unpack_v2_foundation")
    finally:
        engine.dispose()


def test_unpack_v2_foundation_keeps_existing_non_unpack_configuration(tmp_path: Path) -> None:
    database = tmp_path / "unpack-v2-existing.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database)

    command.upgrade(config, "0034_cookiecloud_cron_v1010")
    engine = create_engine(sqlite_database_url(database))
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO downloader "
                    "(id, name, type, base_url, secret_id, monitor_rules, path_mappings, "
                    "capabilities, connection_status, path_mapping_status, enabled, version, "
                    "last_test_at, last_path_diagnostic_at, created_at, updated_at) "
                    "VALUES "
                    "('keep-downloader', '保留下载器', 'QBITTORRENT', 'http://example.invalid', "
                    "NULL, '{}', '[]', '{}', 'UNTESTED', 'UNTESTED', 0, 1, NULL, NULL, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(config, "0035_unpack_v2_foundation")

    engine = create_engine(sqlite_database_url(database))
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT name FROM downloader WHERE id='keep-downloader'")
                ).scalar_one()
                == "保留下载器"
            )
    finally:
        engine.dispose()


def test_unpack_source_scan_migration_adds_transient_scan_tables(tmp_path: Path) -> None:
    database = tmp_path / "unpack-source-scans.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database)

    command.upgrade(config, "0036_unpack_source_scans")

    engine = create_engine(sqlite_database_url(database))
    try:
        tables = set(inspect(engine).get_table_names())
        assert {"unpack_source_scan", "unpack_source_scan_item"} <= tables
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("0036_unpack_source_scans")
    finally:
        engine.dispose()


def test_unpack_selected_snapshot_migration_adds_snapshot_column(tmp_path: Path) -> None:
    database = tmp_path / "unpack-selected-snapshot.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database)

    command.upgrade(config, "0037_unpack_selected_snapshot")

    engine = create_engine(sqlite_database_url(database))
    try:
        columns = {
            item["name"]
            for item in inspect(engine).get_columns("unpack_definition_selected_source")
        }
        assert "source_snapshot" in columns
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("0037_unpack_selected_snapshot")
    finally:
        engine.dispose()


def test_unpack_monitor_schedule_migration_adds_schedule_columns(tmp_path: Path) -> None:
    database = tmp_path / "unpack-monitor-schedule.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database)

    command.upgrade(config, "0038_unpack_monitor_schedule")

    engine = create_engine(sqlite_database_url(database))
    try:
        columns = {item["name"] for item in inspect(engine).get_columns("unpack_definition")}
        assert {"next_run_at", "last_triggered_at"} <= columns
        indexes = {item["name"] for item in inspect(engine).get_indexes("unpack_definition")}
        assert "ix_unpack_definition_status_next_run" in indexes
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("0038_unpack_monitor_schedule")
    finally:
        engine.dispose()


def test_unpack_external_operation_migration_adds_v2_journal(tmp_path: Path) -> None:
    database = tmp_path / "unpack-external-operation.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database)

    command.upgrade(config, "0039_unpack_external_operation")

    engine = create_engine(sqlite_database_url(database))
    try:
        inspector = inspect(engine)
        assert "unpack_external_operation_journal" in set(inspector.get_table_names())
        columns = {
            item["name"] for item in inspector.get_columns("unpack_external_operation_journal")
        }
        assert {
            "id",
            "item_id",
            "idempotency_key",
            "operation_type",
            "target",
            "intent",
            "status",
            "before_snapshot",
            "after_snapshot",
            "last_error_code",
            "created_at",
            "updated_at",
        } <= columns
        indexes = {
            item["name"] for item in inspector.get_indexes("unpack_external_operation_journal")
        }
        assert "ix_unpack_external_operation_item_status" in indexes
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("0039_unpack_external_operation")
    finally:
        engine.dispose()


def test_unpack_execution_plan_migration_adds_frozen_plan_columns(tmp_path: Path) -> None:
    database = tmp_path / "unpack-execution-plan.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database)

    command.upgrade(config, "0040_unpack_execution_plan")

    engine = create_engine(sqlite_database_url(database))
    try:
        columns = {item["name"] for item in inspect(engine).get_columns("unpack_execution_item")}
        assert {
            "execution_plan",
            "execution_plan_digest",
            "execution_plan_created_at",
        } <= columns
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("0040_unpack_execution_plan")
    finally:
        engine.dispose()


def test_unpack_execution_state_migration_adds_restart_checkpoint(tmp_path: Path) -> None:
    database = tmp_path / "unpack-execution-state.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database)

    command.upgrade(config, "0041_unpack_execution_state")

    engine = create_engine(sqlite_database_url(database))
    try:
        columns = {item["name"] for item in inspect(engine).get_columns("unpack_execution_item")}
        assert "execution_state" in columns
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "0041_unpack_execution_state"
            )
    finally:
        engine.dispose()


def test_unpack_review_idempotency_migration_adds_unique_key(tmp_path: Path) -> None:
    database = tmp_path / "unpack-review-idempotency.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database)

    command.upgrade(config, "0042_unpack_review_idempotency")

    engine = create_engine(sqlite_database_url(database))
    try:
        inspector = inspect(engine)
        columns = {item["name"] for item in inspector.get_columns("unpack_review_decision")}
        assert "idempotency_key" in columns
        constraints = {
            item["name"] for item in inspector.get_unique_constraints("unpack_review_decision")
        }
        assert "uq_unpack_review_idempotency_key" in constraints
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "0042_unpack_review_idempotency"
            )
    finally:
        engine.dispose()


def test_unpack_match_origin_migration_adds_persistent_match_source(tmp_path: Path) -> None:
    database = tmp_path / "unpack-match-origin.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database)

    command.upgrade(config, "0043_unpack_match_origin")

    engine = create_engine(sqlite_database_url(database))
    try:
        columns = {item["name"] for item in inspect(engine).get_columns("unpack_execution_item")}
        assert "match_origin" in columns
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "0043_unpack_match_origin"
            )
    finally:
        engine.dispose()


def test_legacy_task_runtime_retirement_disables_telegram_approval(tmp_path: Path) -> None:
    database = tmp_path / "retire-legacy-runtime.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database)

    command.upgrade(config, "0043_unpack_match_origin")
    engine = create_engine(sqlite_database_url(database))
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO ai_channel_binding "
                    "(id, kind, notification_channel_id, enabled, approval_enabled, "
                    "allowed_chat_ids, allowed_user_ids, idle_timeout_minutes, "
                    "max_context_messages, last_update_id, version, created_at, updated_at) "
                    "VALUES ('telegram-retired-test', 'TELEGRAM', NULL, 0, 1, "
                    "'[]', '[]', 60, 20, 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(config, "0044_retire_legacy_task_runtime")

    engine = create_engine(sqlite_database_url(database))
    try:
        with engine.connect() as connection:
            approval_enabled, version = connection.execute(
                text(
                    "SELECT approval_enabled, version FROM ai_channel_binding "
                    "WHERE id='telegram-retired-test'"
                )
            ).one()
            assert approval_enabled == 0
            assert version == 2
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "0044_retire_legacy_task_runtime"
            )
    finally:
        engine.dispose()
