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


def test_template_library_migration_preserves_existing_unpack_history(tmp_path: Path) -> None:
    database = tmp_path / "template-library-preserves-history.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database)
    command.upgrade(config, "0049_site_risk_control")

    engine = create_engine(sqlite_database_url(database))
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO unpack_definition "
                    "(id, name, trigger_kind, status, source_kind, execution_scope_kind, "
                    "source_config, file_filter, site_ids, output_config, retry_enabled, "
                    "max_retries, auto_match_threshold_bps, auto_match_enabled, version, "
                    "created_at, updated_at) VALUES "
                    "('keep-definition', 'Keep existing task', 'MANUAL', 'PENDING_EXECUTION', "
                    "'DIRECTORY', 'ALL_MATCHING_MEDIA', '{\"directory_path\":\"/tmp\"}', "
                    "'{}', '[]', '{}', 1, 3, 9900, 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO unpack_definition_selected_source "
                    "(id, definition_id, source_object_key, canonical_path_hint, filename, "
                    "size_bytes_at_selection, source_snapshot, created_at) VALUES "
                    "('keep-selected-source', 'keep-definition', 'source-1', '/tmp/Movie.mkv', "
                    "'Movie.mkv', 100, '{}', CURRENT_TIMESTAMP)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO unpack_execution "
                    "(id, definition_id, trigger, status, config_snapshot, discovery_complete, "
                    "total_count, matched_auto_count, review_count, content_verified_count, "
                    "content_mismatch_count, timeout_count, error_count, completed_count, "
                    "started_at, version, created_at, updated_at) VALUES "
                    "('keep-execution', 'keep-definition', 'MANUAL', 'DISCOVERING', '{}', 0, "
                    "1, 0, 0, 0, 0, 0, 0, 0, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, "
                    "CURRENT_TIMESTAMP)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO unpack_execution_item "
                    "(id, execution_id, source_object_key, source_snapshot, media_identity, "
                    "status, candidate_generation, retry_count, version, created_at, updated_at) "
                    "VALUES ('keep-item', 'keep-execution', 'source-1', '{}', '{}', "
                    "'DISCOVERED', 0, 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(sqlite_database_url(database))
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "0051_unpack_template_match_acceptance"
            )
            template_import_columns = {
                item["name"] for item in inspect(engine).get_columns("unpack_template_import_job")
            }
            assert "seed_mappings" in template_import_columns
            assert connection.execute(
                text(
                    "SELECT name, task_kind, template_id, template_version "
                    "FROM unpack_definition WHERE id='keep-definition'"
                )
            ).one() == ("Keep existing task", "MANUAL", None, None)
            assert (
                connection.execute(
                    text(
                        "SELECT id FROM unpack_definition_selected_source "
                        "WHERE id='keep-selected-source'"
                    )
                ).scalar_one()
                == "keep-selected-source"
            )
            assert (
                connection.execute(
                    text("SELECT id FROM unpack_execution WHERE id='keep-execution'")
                ).scalar_one()
                == "keep-execution"
            )
            assert (
                connection.execute(
                    text("SELECT id FROM unpack_execution_item WHERE id='keep-item'")
                ).scalar_one()
                == "keep-item"
            )
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
            template_fk_targets = {
                row[2]
                for row in connection.execute(text("PRAGMA foreign_key_list(unpack_definition)"))
            }
            assert "unpack_template" in template_fk_targets
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
