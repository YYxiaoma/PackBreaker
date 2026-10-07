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
