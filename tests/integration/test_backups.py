from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.app.config import AppSettings
from backend.app.infrastructure import backups as backup_module
from backend.app.infrastructure.backups import (
    BackupError,
    apply_backup_retention,
    create_consistent_backup,
    plan_backup_retention,
    restore_consistent_backup,
    verify_backup,
)
from backend.app.infrastructure.runtime import RuntimeManager


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        secret_key_file=None,
    )


def test_consistent_backup_reads_committed_wal_without_copying_master_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runtime = RuntimeManager(settings)
    runtime.start()
    try:
        assert runtime.engine is not None
        with runtime.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE backup_probe (value TEXT NOT NULL)")
            connection.exec_driver_sql(
                "INSERT INTO backup_probe(value) VALUES ('committed-in-wal')"
            )

        artifact = create_consistent_backup(
            settings.database_path,
            settings.config_dir / "backups",
            app_version="0.1.0-test",
        )
    finally:
        runtime.stop()

    verified = verify_backup(artifact.database_path, artifact.manifest_path)
    assert verified.database_sha256 == artifact.database_sha256
    assert verified.alembic_revision == "0024_task_center_v015"
    assert verified.app_version == "0.1.0-test"
    assert artifact.database_path.stat().st_mode & 0o777 == 0o600
    assert artifact.manifest_path.stat().st_mode & 0o777 == 0o600
    assert settings.resolved_secret_key_file.name not in artifact.manifest_path.read_text(
        encoding="utf-8"
    )
    assert {path.name for path in artifact.database_path.parent.iterdir()} == {
        artifact.database_path.name,
        artifact.manifest_path.name,
    }

    uri = f"file:{artifact.database_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        assert connection.execute("SELECT value FROM backup_probe").fetchone() == (
            "committed-in-wal",
        )


def test_verify_backup_rejects_tampered_database(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runtime = RuntimeManager(settings)
    runtime.start()
    runtime.stop()
    artifact = create_consistent_backup(
        settings.database_path,
        settings.config_dir / "backups",
        app_version="0.1.0-test",
    )

    with artifact.database_path.open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(BackupError, match="摘要校验失败"):
        verify_backup(artifact.database_path, artifact.manifest_path)


def test_verify_backup_rejects_manifest_revision_mismatch(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runtime = RuntimeManager(settings)
    runtime.start()
    runtime.stop()
    artifact = create_consistent_backup(
        settings.database_path,
        settings.config_dir / "backups",
        app_version="0.1.0-test",
    )
    manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
    manifest["alembic_revision"] = "wrong-revision"
    artifact.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError, match="migration revision"):
        verify_backup(artifact.database_path, artifact.manifest_path)


def test_restore_replaces_current_database_and_keeps_pre_restore_snapshot(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runtime = RuntimeManager(settings)
    runtime.start()
    try:
        assert runtime.engine is not None
        with runtime.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE restore_probe (value TEXT NOT NULL)")
            connection.exec_driver_sql("INSERT INTO restore_probe(value) VALUES ('from-backup')")
        artifact = create_consistent_backup(
            settings.database_path,
            settings.config_dir / "backups",
            app_version="0.1.0-test",
        )
        with runtime.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE restore_probe SET value='current-before-restore'")
    finally:
        runtime.stop()

    result = restore_consistent_backup(
        artifact.database_path,
        artifact.manifest_path,
        target_database_path=settings.database_path,
        instance_lock_path=settings.instance_lock_path,
        safety_backup_dir=settings.config_dir / "backups" / "pre-restore",
        app_version="0.1.0-test",
    )

    assert result.restored_revision == "0024_task_center_v015"
    assert result.safety_backup is not None
    assert _probe_value(settings.database_path) == "from-backup"
    assert _probe_value(result.safety_backup.database_path) == "current-before-restore"

    probe_runtime = RuntimeManager(settings)
    probe_runtime.start()
    try:
        assert probe_runtime.readiness().ready is True
    finally:
        probe_runtime.stop()


def test_restore_is_blocked_while_runtime_holds_instance_lock(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runtime = RuntimeManager(settings)
    runtime.start()
    try:
        artifact = create_consistent_backup(
            settings.database_path,
            settings.config_dir / "backups",
            app_version="0.1.0-test",
        )
        with pytest.raises(BackupError, match="停止活动 PackBreaker 实例"):
            restore_consistent_backup(
                artifact.database_path,
                artifact.manifest_path,
                target_database_path=settings.database_path,
                instance_lock_path=settings.instance_lock_path,
                safety_backup_dir=settings.config_dir / "backups" / "pre-restore",
                app_version="0.1.0-test",
            )
    finally:
        runtime.stop()


def test_restore_rolls_back_current_database_when_post_switch_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    runtime = RuntimeManager(settings)
    runtime.start()
    try:
        assert runtime.engine is not None
        with runtime.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE restore_probe (value TEXT NOT NULL)")
            connection.exec_driver_sql("INSERT INTO restore_probe(value) VALUES ('from-backup')")
        artifact = create_consistent_backup(
            settings.database_path,
            settings.config_dir / "backups",
            app_version="0.1.0-test",
        )
        with runtime.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE restore_probe SET value='must-survive-failure'")
    finally:
        runtime.stop()

    original_validate = backup_module._validate_sqlite_backup

    def fail_only_after_target_switch(path: Path) -> str | None:
        if path == settings.database_path:
            raise BackupError("synthetic post-switch validation failure")
        return original_validate(path)

    monkeypatch.setattr(backup_module, "_validate_sqlite_backup", fail_only_after_target_switch)
    with pytest.raises(BackupError, match="synthetic post-switch"):
        restore_consistent_backup(
            artifact.database_path,
            artifact.manifest_path,
            target_database_path=settings.database_path,
            instance_lock_path=settings.instance_lock_path,
            safety_backup_dir=settings.config_dir / "backups" / "pre-restore",
            app_version="0.1.0-test",
        )

    assert _probe_value(settings.database_path) == "must-survive-failure"


def test_restore_does_not_modify_current_database_when_atomic_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    runtime = RuntimeManager(settings)
    runtime.start()
    try:
        assert runtime.engine is not None
        with runtime.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE restore_probe (value TEXT NOT NULL)")
            connection.exec_driver_sql("INSERT INTO restore_probe(value) VALUES ('from-backup')")
        artifact = create_consistent_backup(
            settings.database_path,
            settings.config_dir / "backups",
            app_version="0.1.0-test",
        )
        with runtime.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE restore_probe SET value='must-remain-current'")
    finally:
        runtime.stop()

    before = settings.database_path.read_bytes()
    original_replace = os.replace

    def fail_only_target_replace(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == settings.database_path:
            raise OSError("synthetic atomic replace failure")
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_only_target_replace)
    with pytest.raises(BackupError, match="数据库恢复切换失败"):
        restore_consistent_backup(
            artifact.database_path,
            artifact.manifest_path,
            target_database_path=settings.database_path,
            instance_lock_path=settings.instance_lock_path,
            safety_backup_dir=settings.config_dir / "backups" / "pre-restore",
            app_version="0.1.0-test",
        )

    assert settings.database_path.read_bytes() == before
    assert _probe_value(settings.database_path) == "must-remain-current"


def test_backup_retention_only_deletes_expired_verified_root_pairs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runtime = RuntimeManager(settings)
    runtime.start()
    runtime.stop()
    backup_dir = settings.config_dir / "backups"
    now = datetime(2026, 9, 14, tzinfo=UTC)
    old = create_consistent_backup(
        settings.database_path,
        backup_dir,
        app_version="0.1.0-test",
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    recent = create_consistent_backup(
        settings.database_path,
        backup_dir,
        app_version="0.1.0-test",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    newest = create_consistent_backup(
        settings.database_path,
        backup_dir,
        app_version="0.1.0-test",
        created_at=datetime(2026, 9, 13, tzinfo=UTC),
    )
    foreign = backup_dir / "notes.txt"
    foreign.write_text("must survive", encoding="utf-8")
    safety_dir = backup_dir / "pre-restore"
    safety = create_consistent_backup(
        settings.database_path,
        safety_dir,
        app_version="0.1.0-test",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )

    plan = plan_backup_retention(backup_dir, retention_days=30, keep_latest=1, now=now)

    actions = {item.stem: item.action for item in plan.items}
    assert actions[old.database_path.stem] == "delete"
    assert actions[recent.database_path.stem] == "keep"
    assert actions[newest.database_path.stem] == "keep"
    assert plan.ignored_entry_count == 2

    result = apply_backup_retention(backup_dir, retention_days=30, keep_latest=1, now=now)
    assert result.deleted_stems == (old.database_path.stem,)
    assert not old.database_path.exists()
    assert not old.manifest_path.exists()
    assert recent.database_path.exists()
    assert newest.database_path.exists()
    assert foreign.read_text(encoding="utf-8") == "must survive"
    assert safety.database_path.exists()
    assert safety.manifest_path.exists()


def test_backup_retention_blocks_orphan_and_tampered_pairs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runtime = RuntimeManager(settings)
    runtime.start()
    runtime.stop()
    backup_dir = settings.config_dir / "backups"
    artifact = create_consistent_backup(
        settings.database_path,
        backup_dir,
        app_version="0.1.0-test",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    artifact.database_path.write_bytes(b"tampered")
    orphan = backup_dir / "packbreaker-20260501T000000Z-deadbeef.json"
    orphan.write_text("{}", encoding="utf-8")

    plan = plan_backup_retention(
        backup_dir,
        retention_days=30,
        keep_latest=1,
        now=datetime(2026, 9, 14, tzinfo=UTC),
    )

    blocked = {item.stem: item.reason for item in plan.items if item.action == "blocked"}
    assert blocked[artifact.database_path.stem] == "backup_verification_failed"
    assert blocked[orphan.stem] == "backup_pair_incomplete"
    assert plan.delete_count == 0


def _probe_value(database_path: Path) -> str:
    uri = f"file:{database_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute("SELECT value FROM restore_probe").fetchone()
    assert row is not None
    assert isinstance(row[0], str)
    return row[0]
