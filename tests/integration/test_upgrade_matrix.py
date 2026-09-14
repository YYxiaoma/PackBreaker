from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from backend.app.config import AppSettings
from backend.app.infrastructure import runtime as runtime_module
from backend.app.infrastructure import upgrades as upgrades_module
from backend.app.infrastructure.backups import BackupError, validate_sqlite_database, verify_backup
from backend.app.infrastructure.persistence.database import sqlite_database_url
from backend.app.infrastructure.runtime import InstanceLock, RuntimeManager, make_migration_config
from backend.app.infrastructure.upgrades import upgrade_database_safely


def _historical_revisions() -> tuple[str, ...]:
    config = make_migration_config("sqlite+pysqlite:///:memory:")
    revisions = list(reversed(list(ScriptDirectory.from_config(config).walk_revisions())))
    return tuple(revision.revision for revision in revisions[:-1])


@pytest.mark.parametrize("source_revision", _historical_revisions())
def test_every_historical_revision_upgrades_to_head_without_losing_probe(
    tmp_path: Path,
    source_revision: str,
) -> None:
    database = tmp_path / f"{source_revision}.db"
    command.upgrade(make_migration_config(sqlite_database_url(database)), source_revision)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE m6_upgrade_probe (value TEXT NOT NULL)")
        connection.execute("INSERT INTO m6_upgrade_probe(value) VALUES (?)", (source_revision,))
        connection.commit()

    lock = InstanceLock(tmp_path / f"{source_revision}.lock")
    lock.acquire()
    try:
        result = upgrade_database_safely(
            database,
            instance_lock=lock,
            safety_backup_dir=tmp_path / "backups" / source_revision,
            app_version="0.1.0-test",
        )
    finally:
        lock.release()

    assert result.upgraded is True
    assert result.source_revision == source_revision
    assert result.target_revision == "0023_backup_policy"
    assert result.safety_backup is not None
    assert (
        verify_backup(
            result.safety_backup.database_path,
            result.safety_backup.manifest_path,
        ).alembic_revision
        == source_revision
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM m6_upgrade_probe").fetchone() == (
            source_revision,
        )
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0023_backup_policy",
        )


def test_failed_initial_install_does_not_leave_partial_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "packbreaker.db"

    def fail_migration(_database_url: str) -> None:
        raise RuntimeError("synthetic initial migration failure")

    monkeypatch.setattr(runtime_module, "migrate_database", fail_migration)
    lock = InstanceLock(tmp_path / "packbreaker.lock")
    lock.acquire()
    try:
        with pytest.raises(BackupError, match="当前数据库未切换"):
            upgrade_database_safely(
                database,
                instance_lock=lock,
                safety_backup_dir=tmp_path / "backups",
                app_version="0.1.0-test",
            )
    finally:
        lock.release()

    assert not database.exists()
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


def test_upgrade_requires_instance_lock(tmp_path: Path) -> None:
    database = tmp_path / "packbreaker.db"
    command.upgrade(
        make_migration_config(sqlite_database_url(database)), "0021_history_episode_grouping"
    )
    lock = InstanceLock(tmp_path / "packbreaker.lock")

    with pytest.raises(BackupError, match="实例锁"):
        upgrade_database_safely(
            database,
            instance_lock=lock,
            safety_backup_dir=tmp_path / "backups",
            app_version="0.1.0-test",
        )


def test_failed_upgrade_does_not_switch_current_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "packbreaker.db"
    command.upgrade(
        make_migration_config(sqlite_database_url(database)), "0021_history_episode_grouping"
    )
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE m6_upgrade_probe (value TEXT NOT NULL)")
        connection.execute("INSERT INTO m6_upgrade_probe(value) VALUES ('must-survive')")
        connection.commit()
    before = database.read_bytes()

    def fail_migration(_database_url: str) -> None:
        raise RuntimeError("synthetic migration failure")

    monkeypatch.setattr(runtime_module, "migrate_database", fail_migration)
    lock = InstanceLock(tmp_path / "packbreaker.lock")
    lock.acquire()
    try:
        with pytest.raises(BackupError, match="当前数据库未切换"):
            upgrade_database_safely(
                database,
                instance_lock=lock,
                safety_backup_dir=tmp_path / "backups",
                app_version="0.1.0-test",
            )
    finally:
        lock.release()

    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM m6_upgrade_probe").fetchone() == (
            "must-survive",
        )
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0021_history_episode_grouping",
        )


def test_post_switch_validation_failure_restores_pre_upgrade_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "packbreaker.db"
    command.upgrade(
        make_migration_config(sqlite_database_url(database)), "0021_history_episode_grouping"
    )
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE m6_upgrade_probe (value TEXT NOT NULL)")
        connection.execute("INSERT INTO m6_upgrade_probe(value) VALUES ('rollback-me')")
        connection.commit()

    original_validate = validate_sqlite_database
    target_validations = 0

    def fail_only_first_post_switch(path: Path) -> str | None:
        nonlocal target_validations
        if path == database:
            target_validations += 1
            if target_validations == 2:
                raise BackupError("synthetic post-switch validation failure")
        return original_validate(path)

    monkeypatch.setattr(upgrades_module, "validate_sqlite_database", fail_only_first_post_switch)
    lock = InstanceLock(tmp_path / "packbreaker.lock")
    lock.acquire()
    try:
        with pytest.raises(BackupError, match="synthetic post-switch"):
            upgrade_database_safely(
                database,
                instance_lock=lock,
                safety_backup_dir=tmp_path / "backups",
                app_version="0.1.0-test",
            )
    finally:
        lock.release()

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM m6_upgrade_probe").fetchone() == (
            "rollback-me",
        )
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0021_history_episode_grouping",
        )


def test_runtime_start_uses_safe_upgrade_and_keeps_pre_upgrade_snapshot(tmp_path: Path) -> None:
    settings = AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
    )
    settings.config_dir.mkdir(parents=True)
    settings.data_dir.mkdir(parents=True)
    command.upgrade(
        make_migration_config(sqlite_database_url(settings.database_path)),
        "0021_history_episode_grouping",
    )

    runtime = RuntimeManager(settings)
    runtime.start()
    try:
        assert runtime.readiness().ready is True
        assert runtime.readiness().current_revision == "0023_backup_policy"
    finally:
        runtime.stop()

    backups = sorted((settings.config_dir / "backups" / "pre-upgrade").glob("*.db"))
    manifests = sorted((settings.config_dir / "backups" / "pre-upgrade").glob("*.json"))
    assert len(backups) == 1
    assert len(manifests) == 1
    assert (
        verify_backup(backups[0], manifests[0]).alembic_revision == "0021_history_episode_grouping"
    )
