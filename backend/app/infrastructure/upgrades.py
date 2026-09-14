from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from alembic.script import ScriptDirectory

from backend.app.infrastructure.backups import (
    BackupArtifact,
    BackupError,
    create_consistent_backup,
    validate_sqlite_database,
)
from backend.app.infrastructure.persistence.database import sqlite_database_url


class HeldInstanceLock(Protocol):
    @property
    def held(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class DatabaseUpgradeResult:
    source_revision: str | None
    target_revision: str | None
    upgraded: bool
    safety_backup: BackupArtifact | None


def upgrade_database_safely(
    database_path: Path,
    *,
    instance_lock: HeldInstanceLock,
    safety_backup_dir: Path,
    app_version: str,
) -> DatabaseUpgradeResult:
    """在已持有实例锁时，以临时副本完成迁移并原子切换当前 SQLite 数据库。"""

    if not instance_lock.held:
        raise BackupError("数据库升级要求先持有 PackBreaker 实例锁")

    from backend.app.infrastructure.runtime import make_migration_config, migrate_database

    database_url = sqlite_database_url(database_path)
    target_revision = ScriptDirectory.from_config(
        make_migration_config(database_url)
    ).get_current_head()

    source_revision: str | None = None
    safety_backup: BackupArtifact | None = None
    if database_path.exists():
        source_revision = validate_sqlite_database(database_path)
        if source_revision == target_revision:
            return DatabaseUpgradeResult(source_revision, target_revision, False, None)
        safety_backup = create_consistent_backup(
            database_path,
            safety_backup_dir,
            app_version=app_version,
        )

    parent = database_path.parent.resolve(strict=True)
    upgrade_temp = parent / f".{database_path.name}.upgrade-{uuid4().hex}.tmp"
    rollback_temp = parent / f".{database_path.name}.upgrade-rollback-{uuid4().hex}.tmp"
    replaced = False
    try:
        if safety_backup is not None:
            _copy_database_file(safety_backup.database_path, upgrade_temp)
        try:
            migrate_database(sqlite_database_url(upgrade_temp))
        except Exception as exc:
            raise BackupError("数据库升级副本迁移失败，当前数据库未切换") from exc
        _normalize_sqlite_database(upgrade_temp)
        migrated_revision = validate_sqlite_database(upgrade_temp)
        if migrated_revision != target_revision:
            raise BackupError("数据库升级副本 revision 与代码 head 不一致")

        try:
            os.replace(upgrade_temp, database_path)
            replaced = True
            _remove_sqlite_sidecars(database_path)
            _fsync_directory(parent)
            current_revision = validate_sqlite_database(database_path)
            if current_revision != target_revision:
                raise BackupError("数据库升级切换后 revision 与代码 head 不一致")
        except Exception as exc:
            if replaced:
                try:
                    if safety_backup is None:
                        database_path.unlink(missing_ok=True)
                        _remove_sqlite_sidecars(database_path)
                    else:
                        _copy_database_file(safety_backup.database_path, rollback_temp)
                        os.replace(rollback_temp, database_path)
                        _remove_sqlite_sidecars(database_path)
                        if validate_sqlite_database(database_path) != source_revision:
                            raise BackupError("数据库升级失败后的安全快照回滚 revision 不一致")
                    _fsync_directory(parent)
                except Exception as rollback_exc:
                    raise BackupError("数据库升级失败，且恢复升级前安全快照失败") from rollback_exc
            if isinstance(exc, BackupError):
                raise
            raise BackupError("数据库升级原子切换失败") from exc

        return DatabaseUpgradeResult(
            source_revision=source_revision,
            target_revision=target_revision,
            upgraded=True,
            safety_backup=safety_backup,
        )
    finally:
        upgrade_temp.unlink(missing_ok=True)
        rollback_temp.unlink(missing_ok=True)
        _remove_sqlite_sidecars(upgrade_temp)
        _remove_sqlite_sidecars(rollback_temp)


def _copy_database_file(source: Path, destination: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(destination, flags, 0o600)
    try:
        with source.open("rb") as source_handle, os.fdopen(fd, "wb", closefd=True) as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _normalize_sqlite_database(path: Path) -> None:
    uri = f"file:{path.resolve(strict=True).as_posix()}?mode=rw"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            journal_mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
            if journal_mode is None or str(journal_mode[0]).casefold() != "delete":
                raise BackupError("升级副本无法切换为自包含 journal 模式")
    except sqlite3.Error as exc:
        raise BackupError("升级副本 SQLite 规范化失败") from exc
    _remove_sqlite_sidecars(path)
    os.chmod(path, 0o600)
    _fsync_file(path)


def _remove_sqlite_sidecars(path: Path) -> None:
    Path(f"{path}-wal").unlink(missing_ok=True)
    Path(f"{path}-shm").unlink(missing_ok=True)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
