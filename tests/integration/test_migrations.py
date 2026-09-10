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
        "preflight_snapshot",
        "task_unit",
        "task_candidate",
        "task_review_revision",
        "task_review_verification",
        "task_execution_gate",
        "task_execution_plan",
    }.issubset(set(inspector.get_table_names()))
    task_unique_names = {
        constraint["name"] for constraint in inspector.get_unique_constraints("unpack_task")
    }
    assert task_unique_names == {"uq_unpack_task_idempotency_key"}
    assert {
        constraint["name"] for constraint in inspector.get_unique_constraints("operation_journal")
    } == {"uq_operation_journal_idempotency_key"}
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
    site_columns = {column["name"] for column in inspector.get_columns("site")}
    assert "credential_kind" in site_columns
    site_checks = {constraint["name"] for constraint in inspector.get_check_constraints("site")}
    assert {"ck_site_type", "ck_site_credential_kind"}.issubset(site_checks)
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
