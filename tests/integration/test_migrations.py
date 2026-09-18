from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from backend.app.infrastructure.persistence.database import sqlite_database_url


def test_alembic_upgrade_creates_m1_core_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "migration-test.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database_path)

    command.upgrade(config, "head")
    command.check(config)

    engine = create_engine(sqlite_database_url(database_path))
    inspector = inspect(engine)
    assert {
        "administrator",
        "admin_session",
        "api_token",
        "downloader",
        "site",
        "secret",
        "unpack_task",
        "task_event",
        "operation_journal",
        "operation_journal_tombstone",
        "preflight_snapshot",
        "task_unit",
        "task_candidate",
        "task_review_revision",
        "task_review_verification",
        "task_execution_gate",
        "task_execution_plan",
        "task_risk_summary",
        "task_approval",
        "notification_channel",
        "notification_outbox",
        "admin_notification",
        "ai_agent_setting",
        "ai_channel_binding",
        "ai_conversation",
        "ai_message",
        "history_scan",
        "history_scan_file",
        "history_scan_materialization",
        "task_definition",
        "task_schedule",
        "task_source",
        "task_filter",
        "task_output_policy",
        "task_execution_policy",
        "task_execution",
        "task_execution_item",
        "task_execution_event",
    }.issubset(set(inspector.get_table_names()))
    task_unique_names = {
        constraint["name"] for constraint in inspector.get_unique_constraints("unpack_task")
    }
    assert task_unique_names == {"uq_unpack_task_idempotency_key"}
    task_indexes = {index["name"]: index for index in inspector.get_indexes("unpack_task")}
    assert task_indexes["uq_unpack_task_logical_run"]["unique"] == 1
    assert task_indexes["ix_unpack_task_parent_task_id"]["unique"] == 0
    task_columns = {column["name"]: column for column in inspector.get_columns("unpack_task")}
    assert task_columns["parent_task_id"]["nullable"] is True
    assert task_columns["run_number"]["nullable"] is False
    task_definition_columns = {
        column["name"]: column for column in inspector.get_columns("task_definition")
    }
    assert task_definition_columns["site_id"]["nullable"] is True
    task_execution_columns = {
        column["name"]: column for column in inspector.get_columns("task_execution")
    }
    assert task_execution_columns["task_definition_id"]["nullable"] is True
    assert {"success_count", "failed_count", "skipped_count", "config_snapshot"}.issubset(
        task_execution_columns
    )
    task_execution_policy_columns = {
        column["name"] for column in inspector.get_columns("task_execution_policy")
    }
    assert {
        "high_risk_preauthorization_enabled",
        "high_risk_allowed_action_kinds",
    }.issubset(task_execution_policy_columns)
    task_event_columns = {
        column["name"] for column in inspector.get_columns("task_execution_event")
    }
    assert {"event_code", "message", "execution_id", "trace_id", "context"}.issubset(
        task_event_columns
    )
    history_scan_indexes = {index["name"]: index for index in inspector.get_indexes("history_scan")}
    assert history_scan_indexes["ix_history_scan_status_updated_at"]["unique"] == 0
    history_file_indexes = {
        index["name"]: index for index in inspector.get_indexes("history_scan_file")
    }
    assert history_file_indexes["ix_history_scan_file_scan_generation"]["unique"] == 0
    assert {
        constraint["name"] for constraint in inspector.get_unique_constraints("history_scan")
    } == {"uq_history_scan_root_media_kind"}
    assert {
        constraint["name"] for constraint in inspector.get_unique_constraints("history_scan_file")
    } == {"uq_history_scan_file_scan_path"}
    history_scan_checks = {
        constraint["name"]: constraint["sqltext"]
        for constraint in inspector.get_check_constraints("history_scan")
    }
    assert "CANCELLED" in str(history_scan_checks["ck_history_scan_status"])
    history_materialization_indexes = {
        index["name"]: index for index in inspector.get_indexes("history_scan_materialization")
    }
    assert (
        history_materialization_indexes["ix_history_scan_materialization_scan_created_at"]["unique"]
        == 0
    )
    assert (
        history_materialization_indexes["ix_history_scan_materialization_scan_episode_group"][
            "unique"
        ]
        == 0
    )
    history_materialization_columns = {
        column["name"] for column in inspector.get_columns("history_scan_materialization")
    }
    assert {
        "episode_kind",
        "episode_season",
        "episode_start",
        "episode_end",
        "episode_label",
        "episode_group_key",
        "episode_variant_key",
    }.issubset(history_materialization_columns)
    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("history_scan_materialization")
    } == {"uq_history_scan_materialization_file_snapshot"}
    history_materialization_checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("history_scan_materialization")
    }
    assert {
        "ck_history_scan_materialization_status",
        "ck_history_scan_materialization_task_consistency",
    }.issubset(history_materialization_checks)
    assert {
        constraint["name"] for constraint in inspector.get_unique_constraints("operation_journal")
    } == {"uq_operation_journal_idempotency_key"}
    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("operation_journal_tombstone")
    } == {"uq_operation_journal_tombstone_idempotency_key_digest"}
    tombstone_checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("operation_journal_tombstone")
    }
    assert "ck_operation_journal_tombstone_final_status" in tombstone_checks
    assert {
        constraint["name"] for constraint in inspector.get_unique_constraints("admin_session")
    } == {"uq_admin_session_token_digest"}
    assert {constraint["name"] for constraint in inspector.get_unique_constraints("api_token")} == {
        "uq_api_token_token_digest"
    }
    assert {
        constraint["name"] for constraint in inspector.get_unique_constraints("downloader")
    } == {"uq_downloader_name"}
    assert {constraint["name"] for constraint in inspector.get_unique_constraints("site")} == {
        "uq_site_name"
    }
    assert {
        constraint["name"] for constraint in inspector.get_unique_constraints("preflight_snapshot")
    } == {"uq_preflight_snapshot_snapshot_digest"}
    assert {constraint["name"] for constraint in inspector.get_unique_constraints("task_unit")} == {
        "uq_task_unit_task_inventory_key"
    }
    assert {
        constraint["name"] for constraint in inspector.get_unique_constraints("task_candidate")
    } == {"uq_task_candidate_snapshot_site_torrent"}
    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("task_review_verification")
    } == {
        "uq_task_review_verification_revision",
        "uq_task_review_verification_verification_digest",
    }
    assert {
        constraint["name"] for constraint in inspector.get_unique_constraints("task_execution_gate")
    } == {"uq_task_execution_gate_gate_digest"}
    assert {
        constraint["name"] for constraint in inspector.get_unique_constraints("task_execution_plan")
    } == {"uq_task_execution_plan_plan_digest"}
    assert {
        constraint["name"] for constraint in inspector.get_unique_constraints("task_risk_summary")
    } == {
        "uq_task_risk_summary_execution_plan",
        "uq_task_risk_summary_risk_digest",
    }
    assert {
        constraint["name"] for constraint in inspector.get_unique_constraints("task_approval")
    } == {"uq_task_approval_execution_plan"}
    task_approval_columns = {column["name"] for column in inspector.get_columns("task_approval")}
    assert {
        "telegram_notified_at",
        "telegram_chat_id",
        "telegram_message_id",
    }.issubset(task_approval_columns)
    task_approval_checks = [
        str(constraint["sqltext"])
        for constraint in inspector.get_check_constraints("task_approval")
    ]
    assert any("EXPIRED" in value for value in task_approval_checks)
    assert any("TELEGRAM" in value for value in task_approval_checks)
    site_columns = {column["name"] for column in inspector.get_columns("site")}
    assert "credential_kind" in site_columns
    assert {
        "request_timeout_seconds",
        "search_interval_seconds",
        "user_agent",
        "browser_emulation_enabled",
        "proxy_enabled",
        "proxy_host",
        "proxy_port",
        "proxy_username",
        "proxy_secret_id",
    }.issubset(site_columns)
    site_checks = {constraint["name"] for constraint in inspector.get_check_constraints("site")}
    assert {"ck_site_type", "ck_site_credential_kind"}.issubset(site_checks)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO site "
                "(id, name, type, base_url, credential_kind, secret_id, capabilities, "
                "connection_status, enabled, version, last_test_at, created_at, updated_at) "
                "VALUES ('hhclub-site', 'hhclub', 'HHCLUB', 'https://hhanclub.net', "
                "'COOKIE', NULL, "
                "'{}', 'UNTESTED', 0, 1, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    outbox_columns = {
        column["name"]: column for column in inspector.get_columns("notification_outbox")
    }
    assert {"subject_kind", "subject_id"}.issubset(outbox_columns)
    assert outbox_columns["subject_kind"]["nullable"] is False
    assert outbox_columns["subject_id"]["nullable"] is False
    assert outbox_columns["task_id"]["nullable"] is True
    assert outbox_columns["last_event_id"]["nullable"] is True
    assert {
        constraint["name"] for constraint in inspector.get_unique_constraints("notification_outbox")
    } == {"uq_notification_outbox_channel_subject_event_key"}
    administrator_columns = {column["name"] for column in inspector.get_columns("administrator")}
    assert {
        "username",
        "must_change_password",
        "password_changed_at",
        "last_login_at",
    }.issubset(administrator_columns)
    channel_columns = {column["name"] for column in inspector.get_columns("notification_channel")}
    assert {
        "event_types",
        "proxy_enabled",
        "proxy_host",
        "proxy_port",
        "proxy_username",
        "proxy_secret_id",
    }.issubset(channel_columns)
    ai_setting_columns = {column["name"] for column in inspector.get_columns("ai_agent_setting")}
    assert {
        "enabled",
        "provider_kind",
        "base_url",
        "api_key_secret_id",
        "model",
        "request_timeout_seconds",
        "max_context_messages",
        "data_scopes",
        "connection_status",
        "last_test_at",
        "version",
    }.issubset(ai_setting_columns)
    ai_binding_columns = {column["name"] for column in inspector.get_columns("ai_channel_binding")}
    assert "approval_enabled" in ai_binding_columns
    engine.dispose()


def test_v017_database_upgrades_with_high_risk_preauthorization_disabled(tmp_path: Path) -> None:
    database_path = tmp_path / "v017-to-v018.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database_path)
    command.upgrade(config, "0026_ai_agent_v016")

    engine = create_engine(sqlite_database_url(database_path))
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO task_definition "
                "(id, name, kind, status, site_id, version, created_at, updated_at) "
                "VALUES ('definition-v017', 'legacy monitor', 'MONITOR', 'ENABLED', NULL, 1, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO task_execution_policy "
                "(id, task_definition_id, stability_detection_enabled, stability_wait_seconds, "
                "only_completed_downloads, initial_scope, debounce_seconds, overlap_policy, "
                "auto_retry_enabled, max_auto_retries, retry_intervals_seconds, "
                "created_at, updated_at) VALUES "
                "('policy-v017', 'definition-v017', 1, 60, 1, 'NEW_ONLY', 30, 'SKIP', "
                "1, 3, '[60,300,900]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    command.upgrade(config, "head")
    command.check(config)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT high_risk_preauthorization_enabled, high_risk_allowed_action_kinds "
                "FROM task_execution_policy WHERE id = 'policy-v017'"
            )
        ).one()
    assert row == (0, "[]")
    engine.dispose()


def test_v018_stage_c_database_upgrades_with_telegram_approval_disabled(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stage-c-to-stage-d.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database_path)
    command.upgrade(config, "0027_task_approval_v018")

    engine = create_engine(sqlite_database_url(database_path))
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO ai_channel_binding "
                "(id, kind, notification_channel_id, enabled, allowed_chat_ids, "
                "allowed_user_ids, idle_timeout_minutes, max_context_messages, "
                "last_update_id, version, created_at, updated_at) VALUES "
                "('telegram', 'TELEGRAM', NULL, 0, '[]', '[]', 60, 20, 0, 1, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    command.upgrade(config, "head")
    command.check(config)
    with engine.connect() as connection:
        approval_enabled = connection.scalar(
            text("SELECT approval_enabled FROM ai_channel_binding WHERE id = 'telegram'")
        )
    assert approval_enabled == 0
    engine.dispose()


def test_v017_single_telegram_channel_is_reused_without_enabling_new_capabilities(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "v017-telegram-reuse.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database_path)
    command.upgrade(config, "0026_ai_agent_v016")

    engine = create_engine(sqlite_database_url(database_path))
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO notification_channel "
                "(id, name, type, secret_id, task_link_base_url, aggregation_window_seconds, "
                "event_types, proxy_enabled, proxy_host, proxy_port, proxy_username, "
                "proxy_secret_id, connection_status, enabled, version, last_test_at, "
                "created_at, updated_at) VALUES "
                "('legacy-telegram', 'legacy telegram', 'TELEGRAM', NULL, NULL, 300, "
                "'[]', 1, 'proxy.example', 8080, NULL, NULL, 'OK', 1, 7, NULL, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    command.upgrade(config, "head")
    command.check(config)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT notification_channel_id, enabled, approval_enabled, "
                "allowed_chat_ids, allowed_user_ids, last_update_id, version "
                "FROM ai_channel_binding WHERE id = 'telegram'"
            )
        ).one()
    assert row == ("legacy-telegram", 0, 0, "[]", "[]", 0, 1)
    engine.dispose()


def test_v017_existing_telegram_binding_is_preserved_and_approval_stays_disabled(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "v017-existing-telegram-binding.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database_path)
    command.upgrade(config, "0026_ai_agent_v016")

    engine = create_engine(sqlite_database_url(database_path))
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO notification_channel "
                "(id, name, type, secret_id, task_link_base_url, aggregation_window_seconds, "
                "event_types, proxy_enabled, proxy_host, proxy_port, proxy_username, "
                "proxy_secret_id, connection_status, enabled, version, last_test_at, "
                "created_at, updated_at) VALUES "
                "('legacy-telegram', 'legacy telegram', 'TELEGRAM', NULL, NULL, 300, "
                "'[]', 0, NULL, NULL, NULL, NULL, 'OK', 1, 3, NULL, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO ai_channel_binding "
                "(id, kind, notification_channel_id, enabled, allowed_chat_ids, "
                "allowed_user_ids, idle_timeout_minutes, max_context_messages, "
                "last_update_id, version, created_at, updated_at) VALUES "
                "('telegram', 'TELEGRAM', 'legacy-telegram', 1, :chat_ids, :user_ids, "
                "90, 12, 456, 5, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"chat_ids": '["123"]', "user_ids": '["88"]'},
        )

    command.upgrade(config, "head")
    command.check(config)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT notification_channel_id, enabled, approval_enabled, "
                "allowed_chat_ids, allowed_user_ids, idle_timeout_minutes, "
                "max_context_messages, last_update_id, version "
                "FROM ai_channel_binding WHERE id = 'telegram'"
            )
        ).one()
    assert row == ("legacy-telegram", 1, 0, '["123"]', '["88"]', 90, 12, 456, 5)
    engine.dispose()


def test_v017_multiple_telegram_channels_are_not_ambiguously_auto_bound(tmp_path: Path) -> None:
    database_path = tmp_path / "v017-multiple-telegram.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database_path)
    command.upgrade(config, "0026_ai_agent_v016")

    engine = create_engine(sqlite_database_url(database_path))
    with engine.begin() as connection:
        for channel_id in ("telegram-a", "telegram-b"):
            connection.execute(
                text(
                    "INSERT INTO notification_channel "
                    "(id, name, type, secret_id, task_link_base_url, aggregation_window_seconds, "
                    "event_types, proxy_enabled, connection_status, enabled, version, "
                    "created_at, updated_at) VALUES "
                    "(:id, :name, 'TELEGRAM', NULL, NULL, 300, '[]', 0, 'UNTESTED', "
                    "0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"id": channel_id, "name": channel_id},
            )

    command.upgrade(config, "head")
    command.check(config)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT notification_channel_id, enabled, approval_enabled "
                "FROM ai_channel_binding WHERE id = 'telegram'"
            )
        ).one()
    assert row == (None, 0, 0)
    engine.dispose()


def test_v015_database_upgrades_to_v016_management_foundation(tmp_path: Path) -> None:
    database_path = tmp_path / "v015-to-v016.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database_path)
    command.upgrade(config, "0024_task_center_v015")

    engine = create_engine(sqlite_database_url(database_path))
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO administrator (id, password_hash, created_at, updated_at) "
                "VALUES ('admin', 'legacy-hash', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO site "
                "(id, name, type, base_url, credential_kind, secret_id, capabilities, "
                "connection_status, enabled, version, last_test_at, created_at, updated_at) "
                "VALUES ('legacy-site', 'legacy-site', 'MTEAM', 'https://api.m-team.cc', "
                "'API_KEY', NULL, '{}', 'UNTESTED', 0, 1, NULL, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO notification_channel "
                "(id, name, type, secret_id, task_link_base_url, aggregation_window_seconds, "
                "connection_status, enabled, version, last_test_at, created_at, updated_at) "
                "VALUES ('legacy-channel', 'legacy-channel', 'TELEGRAM', NULL, NULL, 300, "
                "'UNTESTED', 0, 1, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    command.upgrade(config, "head")
    command.check(config)
    with engine.connect() as connection:
        administrator = connection.execute(
            text(
                "SELECT username, must_change_password, password_hash "
                "FROM administrator WHERE id = 'admin'"
            )
        ).one()
        site = connection.execute(
            text(
                "SELECT request_timeout_seconds, search_interval_seconds, proxy_enabled "
                "FROM site WHERE id = 'legacy-site'"
            )
        ).one()
        channel = connection.execute(
            text(
                "SELECT event_types, proxy_enabled FROM notification_channel "
                "WHERE id = 'legacy-channel'"
            )
        ).one()
    assert administrator == ("admin", 0, "legacy-hash")
    assert site == (15, 0, 0)
    assert channel == ("[]", 0)
    engine.dispose()


def test_site_credential_migration_backfills_existing_mteam_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "site-credential-migration.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database_path)
    command.upgrade(config, "0005_sites")

    engine = create_engine(sqlite_database_url(database_path))
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO site "
                "(id, name, type, base_url, secret_id, capabilities, connection_status, enabled, "
                "version, last_test_at, created_at, updated_at) "
                "VALUES ('site-1', 'legacy', 'MTEAM', 'https://api.m-team.cc', NULL, '{}', "
                "'UNTESTED', 0, 1, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    command.upgrade(config, "head")
    with engine.connect() as connection:
        credential_kind = connection.scalar(
            text("SELECT credential_kind FROM site WHERE id = 'site-1'")
        )
    assert credential_kind == "API_KEY"
    engine.dispose()


def test_notification_subject_migration_backfills_existing_task_outbox(tmp_path: Path) -> None:
    database_path = tmp_path / "notification-subject-migration.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = sqlite_database_url(database_path)
    command.upgrade(config, "0014_notifications")

    engine = create_engine(sqlite_database_url(database_path))
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO unpack_task "
                "(id, type, source_downloader_id, source_hash, normalized_unit_key, "
                "idempotency_key, status, trace_id, checkpoint, error_code, version) "
                "VALUES ('task-legacy', 'PACKAGE_UNPACK', 'downloader-legacy', 'hash', 'unit', "
                "'idem-legacy', 'PENDING', '00000000-0000-0000-0000-000000000001', '{}', NULL, 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO task_event "
                "(id, task_id, from_status, to_status, event_type, reason) "
                "VALUES ('event-legacy', 'task-legacy', NULL, 'PENDING', 'TASK_CREATED', 'legacy')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO notification_channel "
                "(id, name, type, secret_id, task_link_base_url, aggregation_window_seconds, "
                "connection_status, enabled, version, last_test_at) "
                "VALUES ('channel-legacy', 'legacy', 'TELEGRAM', NULL, NULL, 300, "
                "'UNTESTED', 0, 1, NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO notification_outbox "
                "(id, channel_id, task_id, last_event_id, channel_version, event_key, title, body, "
                "severity, link, state, pending_count, attempt_count, next_attempt_at, "
                "last_sent_at, "
                "delivered_at, last_error_code) "
                "VALUES ('outbox-legacy', 'channel-legacy', 'task-legacy', 'event-legacy', 1, "
                "'TASK_PENDING', 'legacy', 'legacy', 'INFO', NULL, 'PENDING', 1, 0, "
                "CURRENT_TIMESTAMP, NULL, NULL, NULL)"
            )
        )

    command.upgrade(config, "head")
    command.check(config)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT subject_kind, subject_id, task_id, last_event_id "
                "FROM notification_outbox WHERE id = 'outbox-legacy'"
            )
        ).one()
        task_row = connection.execute(
            text("SELECT parent_task_id, run_number FROM unpack_task WHERE id = 'task-legacy'")
        ).one()
        event_count = connection.scalar(
            text("SELECT COUNT(*) FROM task_event WHERE id = 'event-legacy'")
        )
    assert row == ("TASK", "task-legacy", "task-legacy", "event-legacy")
    assert task_row == (None, 1)
    assert event_count == 1
    engine.dispose()
