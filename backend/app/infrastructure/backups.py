from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

BACKUP_FORMAT_VERSION = 1
_BACKUP_STEM = re.compile(r"^packbreaker-\d{8}T\d{6}Z-[0-9a-f]{8}$")


class BackupError(RuntimeError):
    """备份创建或校验失败。"""


@dataclass(frozen=True, slots=True)
class BackupArtifact:
    database_path: Path
    manifest_path: Path
    created_at: datetime
    database_sha256: str
    database_size_bytes: int
    alembic_revision: str | None
    app_version: str

    def as_dict(self) -> dict[str, object]:
        return {
            "format_version": BACKUP_FORMAT_VERSION,
            "created_at": self.created_at.isoformat().replace("+00:00", "Z"),
            "app_version": self.app_version,
            "database_file": self.database_path.name,
            "manifest_file": self.manifest_path.name,
            "database_sha256": self.database_sha256,
            "database_size_bytes": self.database_size_bytes,
            "alembic_revision": self.alembic_revision,
        }


@dataclass(frozen=True, slots=True)
class RestoreResult:
    restored_from: BackupArtifact
    safety_backup: BackupArtifact | None
    target_database: Path
    restored_revision: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "target_database": self.target_database.name,
            "restored_revision": self.restored_revision,
            "restored_from": self.restored_from.as_dict(),
            "safety_backup": None if self.safety_backup is None else self.safety_backup.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class BackupRetentionItem:
    stem: str
    action: Literal["keep", "delete", "blocked"]
    reason: str
    created_at: datetime | None
    database_path: Path | None
    manifest_path: Path | None
    database_identity: tuple[int, int] | None = None
    manifest_identity: tuple[int, int] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "stem": self.stem,
            "action": self.action,
            "reason": self.reason,
            "created_at": (
                None
                if self.created_at is None
                else self.created_at.isoformat().replace("+00:00", "Z")
            ),
        }


@dataclass(frozen=True, slots=True)
class BackupRetentionPlan:
    retention_days: int
    keep_latest: int
    cutoff: datetime
    items: tuple[BackupRetentionItem, ...]
    ignored_entry_count: int

    @property
    def delete_count(self) -> int:
        return sum(item.action == "delete" for item in self.items)

    @property
    def blocked_count(self) -> int:
        return sum(item.action == "blocked" for item in self.items)

    def as_dict(self) -> dict[str, object]:
        return {
            "retention_days": self.retention_days,
            "keep_latest": self.keep_latest,
            "cutoff": self.cutoff.isoformat().replace("+00:00", "Z"),
            "delete_count": self.delete_count,
            "blocked_count": self.blocked_count,
            "ignored_entry_count": self.ignored_entry_count,
            "items": [item.as_dict() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class BackupRetentionResult:
    plan: BackupRetentionPlan
    deleted_stems: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            **self.plan.as_dict(),
            "deleted_count": len(self.deleted_stems),
            "deleted_stems": list(self.deleted_stems),
        }


def create_consistent_backup(
    database_path: Path,
    backup_dir: Path,
    *,
    app_version: str,
    created_at: datetime | None = None,
) -> BackupArtifact:
    """使用 SQLite Backup API 创建可在 WAL 活跃时读取的一致性数据库快照。"""

    source = database_path.resolve(strict=True)
    _require_regular_file(database_path, label="源数据库")
    target_dir = _ensure_backup_directory(backup_dir)
    timestamp = (created_at or datetime.now(UTC)).astimezone(UTC)
    suffix = uuid4().hex[:8]
    stem = f"packbreaker-{timestamp.strftime('%Y%m%dT%H%M%SZ')}-{suffix}"
    final_database = target_dir / f"{stem}.db"
    final_manifest = target_dir / f"{stem}.json"
    temporary_database = target_dir / f".{stem}.db.tmp"
    temporary_manifest = target_dir / f".{stem}.json.tmp"
    database_published = False
    manifest_published = False

    try:
        _sqlite_backup(source, temporary_database)
        revision = _validate_sqlite_backup(temporary_database)
        database_sha256 = _sha256(temporary_database)
        database_size = temporary_database.stat().st_size
        artifact = BackupArtifact(
            database_path=final_database,
            manifest_path=final_manifest,
            created_at=timestamp,
            database_sha256=database_sha256,
            database_size_bytes=database_size,
            alembic_revision=revision,
            app_version=app_version,
        )
        _write_manifest(temporary_manifest, artifact)
        os.replace(temporary_database, final_database)
        database_published = True
        _fsync_directory(target_dir)
        os.replace(temporary_manifest, final_manifest)
        manifest_published = True
        _fsync_directory(target_dir)
        return artifact
    except Exception:
        temporary_database.unlink(missing_ok=True)
        temporary_manifest.unlink(missing_ok=True)
        if database_published and not manifest_published:
            final_database.unlink(missing_ok=True)
            _fsync_directory(target_dir)
        raise


def verify_backup(database_path: Path, manifest_path: Path) -> BackupArtifact:
    """验证 manifest、数据库摘要、SQLite 完整性与 Alembic revision。"""

    _require_regular_file(database_path, label="备份数据库")
    _require_regular_file(manifest_path, label="备份 manifest")
    manifest = _read_manifest(manifest_path)
    if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        raise BackupError("不支持的备份格式版本")
    if manifest.get("database_file") != database_path.name:
        raise BackupError("manifest 与备份数据库文件名不匹配")

    expected_digest = _required_string(manifest, "database_sha256")
    actual_digest = _sha256(database_path)
    if actual_digest != expected_digest:
        raise BackupError("备份数据库摘要校验失败")

    expected_size = manifest.get("database_size_bytes")
    if not isinstance(expected_size, int) or expected_size < 0:
        raise BackupError("manifest database_size_bytes 无效")
    if database_path.stat().st_size != expected_size:
        raise BackupError("备份数据库大小与 manifest 不一致")

    actual_revision = _validate_sqlite_backup(database_path)
    expected_revision = manifest.get("alembic_revision")
    if expected_revision is not None and not isinstance(expected_revision, str):
        raise BackupError("manifest alembic_revision 无效")
    if actual_revision != expected_revision:
        raise BackupError("备份数据库 migration revision 与 manifest 不一致")

    created_at = _parse_created_at(_required_string(manifest, "created_at"))
    return BackupArtifact(
        database_path=database_path,
        manifest_path=manifest_path,
        created_at=created_at,
        database_sha256=actual_digest,
        database_size_bytes=expected_size,
        alembic_revision=actual_revision,
        app_version=_required_string(manifest, "app_version"),
    )


def validate_sqlite_database(path: Path) -> str | None:
    """只读验证 SQLite 完整性并返回 Alembic revision。"""

    _require_regular_file(path, label="SQLite 数据库")
    return _validate_sqlite_backup(path)


def plan_backup_retention(
    backup_dir: Path,
    *,
    retention_days: int,
    keep_latest: int,
    now: datetime | None = None,
) -> BackupRetentionPlan:
    """仅对备份根目录中的 PackBreaker 标准成对产物生成保留计划。"""

    if retention_days < 1:
        raise BackupError("retention_days 必须至少为 1")
    if keep_latest < 1:
        raise BackupError("keep_latest 必须至少为 1")

    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = timestamp - timedelta(days=retention_days)
    if backup_dir.is_symlink():
        raise BackupError("备份目录不能是符号链接")
    if not backup_dir.exists():
        return BackupRetentionPlan(retention_days, keep_latest, cutoff, (), 0)
    directory = _require_existing_real_directory(backup_dir, label="备份目录")

    stems: set[str] = set()
    ignored_entry_count = 0
    for entry in directory.iterdir():
        if entry.name.endswith((".db", ".json")) and _BACKUP_STEM.fullmatch(entry.stem):
            stems.add(entry.stem)
        else:
            ignored_entry_count += 1

    valid: list[tuple[str, BackupArtifact, tuple[int, int], tuple[int, int]]] = []
    blocked: list[BackupRetentionItem] = []
    for stem in sorted(stems):
        database = directory / f"{stem}.db"
        manifest = directory / f"{stem}.json"
        if not database.exists() or not manifest.exists():
            blocked.append(
                BackupRetentionItem(
                    stem=stem,
                    action="blocked",
                    reason="backup_pair_incomplete",
                    created_at=None,
                    database_path=database if database.exists() else None,
                    manifest_path=manifest if manifest.exists() else None,
                )
            )
            continue
        try:
            artifact = verify_backup(database, manifest)
            database_identity = _file_identity(database, label="备份数据库")
            manifest_identity = _file_identity(manifest, label="备份 manifest")
        except BackupError:
            blocked.append(
                BackupRetentionItem(
                    stem=stem,
                    action="blocked",
                    reason="backup_verification_failed",
                    created_at=None,
                    database_path=database,
                    manifest_path=manifest,
                )
            )
            continue
        valid.append((stem, artifact, database_identity, manifest_identity))

    newest = sorted(valid, key=lambda item: (item[1].created_at, item[0]), reverse=True)
    protected_stems = {stem for stem, *_ in newest[:keep_latest]}
    planned: list[BackupRetentionItem] = []
    for stem, artifact, database_identity, manifest_identity in newest:
        if stem in protected_stems:
            action: Literal["keep", "delete"] = "keep"
            reason = "keep_latest"
        elif artifact.created_at >= cutoff:
            action = "keep"
            reason = "within_retention"
        else:
            action = "delete"
            reason = "expired"
        planned.append(
            BackupRetentionItem(
                stem=stem,
                action=action,
                reason=reason,
                created_at=artifact.created_at,
                database_path=artifact.database_path,
                manifest_path=artifact.manifest_path,
                database_identity=database_identity,
                manifest_identity=manifest_identity,
            )
        )

    return BackupRetentionPlan(
        retention_days=retention_days,
        keep_latest=keep_latest,
        cutoff=cutoff,
        items=tuple([*planned, *blocked]),
        ignored_entry_count=ignored_entry_count,
    )


def apply_backup_retention(
    backup_dir: Path,
    *,
    retention_days: int,
    keep_latest: int,
    now: datetime | None = None,
) -> BackupRetentionResult:
    """重新生成并执行保留计划；只删除仍保持原 inode 身份的已验证成对备份。"""

    plan = plan_backup_retention(
        backup_dir,
        retention_days=retention_days,
        keep_latest=keep_latest,
        now=now,
    )
    if not backup_dir.exists():
        return BackupRetentionResult(plan=plan, deleted_stems=())
    directory = _require_existing_real_directory(backup_dir, label="备份目录")
    deleted: list[str] = []
    for item in plan.items:
        if item.action != "delete":
            continue
        if (
            item.database_path is None
            or item.manifest_path is None
            or item.database_identity is None
            or item.manifest_identity is None
        ):
            raise BackupError("保留计划缺少删除所需的文件身份")
        verify_backup(item.database_path, item.manifest_path)
        _delete_verified_backup_member(
            item.database_path,
            item.database_identity,
            label="备份数据库",
        )
        _delete_verified_backup_member(
            item.manifest_path,
            item.manifest_identity,
            label="备份 manifest",
        )
        _fsync_directory(directory)
        deleted.append(item.stem)
    return BackupRetentionResult(plan=plan, deleted_stems=tuple(deleted))


def restore_consistent_backup(
    backup_database_path: Path,
    manifest_path: Path,
    *,
    target_database_path: Path,
    instance_lock_path: Path,
    safety_backup_dir: Path,
    app_version: str,
) -> RestoreResult:
    """在确认实例锁空闲后恢复数据库；切换失败时自动回滚到恢复前快照。"""

    from backend.app.infrastructure.persistence.database import sqlite_database_url
    from backend.app.infrastructure.runtime import (
        InstanceLock,
        InstanceLockUnavailable,
        migrate_database,
    )

    artifact = verify_backup(backup_database_path, manifest_path)
    target_parent = _ensure_real_directory(target_database_path.parent, label="数据库目录")
    target = target_parent / target_database_path.name
    restore_temp = target_parent / f".{target.name}.restore-{uuid4().hex}.tmp"
    rollback_temp = target_parent / f".{target.name}.rollback-{uuid4().hex}.tmp"
    lock = InstanceLock(instance_lock_path)
    try:
        try:
            lock.acquire()
        except InstanceLockUnavailable as exc:
            raise BackupError("恢复要求先停止活动 PackBreaker 实例") from exc

        safety_backup: BackupArtifact | None = None
        if target.exists():
            _require_regular_file(target, label="当前数据库")
            safety_backup = create_consistent_backup(
                target,
                safety_backup_dir,
                app_version=app_version,
            )

        _copy_database_file(artifact.database_path, restore_temp)
        try:
            migrate_database(sqlite_database_url(restore_temp))
        except Exception as exc:
            raise BackupError("恢复副本数据库迁移预检失败") from exc
        _normalize_sqlite_database(restore_temp)
        restored_revision = _validate_sqlite_backup(restore_temp)

        replaced = False
        try:
            os.replace(restore_temp, target)
            replaced = True
            _remove_sqlite_sidecars(target)
            _fsync_directory(target_parent)
            post_switch_revision = _validate_sqlite_backup(target)
            if post_switch_revision != restored_revision:
                raise BackupError("恢复后 migration revision 与预检结果不一致")
        except Exception as exc:
            if replaced:
                try:
                    if safety_backup is None:
                        target.unlink(missing_ok=True)
                        _remove_sqlite_sidecars(target)
                    else:
                        _copy_database_file(safety_backup.database_path, rollback_temp)
                        os.replace(rollback_temp, target)
                        _remove_sqlite_sidecars(target)
                        if _sha256(target) != safety_backup.database_sha256:
                            raise BackupError("恢复前安全快照回滚后摘要校验失败")
                    _fsync_directory(target_parent)
                except Exception as rollback_exc:
                    raise BackupError("数据库恢复失败，且恢复前状态自动回滚失败") from rollback_exc
            if isinstance(exc, BackupError):
                raise
            raise BackupError("数据库恢复切换失败") from exc

        return RestoreResult(
            restored_from=artifact,
            safety_backup=safety_backup,
            target_database=target,
            restored_revision=restored_revision,
        )
    finally:
        restore_temp.unlink(missing_ok=True)
        rollback_temp.unlink(missing_ok=True)
        _remove_sqlite_sidecars(restore_temp)
        _remove_sqlite_sidecars(rollback_temp)
        lock.release()


def _sqlite_backup(source: Path, destination: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(destination, flags, 0o600)
    os.close(fd)
    source_uri = f"file:{source.as_posix()}?mode=ro"
    try:
        with (
            sqlite3.connect(source_uri, uri=True, timeout=5.0) as source_connection,
            sqlite3.connect(destination, timeout=5.0) as destination_connection,
        ):
            source_connection.backup(destination_connection)
            journal_mode = destination_connection.execute("PRAGMA journal_mode=DELETE").fetchone()
            if journal_mode is None or str(journal_mode[0]).casefold() != "delete":
                raise BackupError("备份数据库无法切换为自包含 journal 模式")
    except sqlite3.Error as exc:
        raise BackupError("SQLite 一致性备份失败") from exc
    finally:
        Path(f"{destination}-wal").unlink(missing_ok=True)
        Path(f"{destination}-shm").unlink(missing_ok=True)
    os.chmod(destination, 0o600)
    _fsync_file(destination)


def _copy_database_file(source: Path, destination: Path) -> None:
    _require_regular_file(source, label="数据库副本来源")
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
    _require_regular_file(path, label="SQLite 数据库")
    uri = f"file:{path.resolve(strict=True).as_posix()}?mode=rw"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            journal_mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
            if journal_mode is None or str(journal_mode[0]).casefold() != "delete":
                raise BackupError("SQLite 数据库无法切换为自包含 journal 模式")
    except sqlite3.Error as exc:
        raise BackupError("SQLite 数据库规范化失败") from exc
    _remove_sqlite_sidecars(path)
    os.chmod(path, 0o600)
    _fsync_file(path)


def _remove_sqlite_sidecars(path: Path) -> None:
    Path(f"{path}-wal").unlink(missing_ok=True)
    Path(f"{path}-shm").unlink(missing_ok=True)


def _validate_sqlite_backup(path: Path) -> str | None:
    uri = f"file:{path.resolve(strict=True).as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            if integrity_rows != [("ok",)]:
                raise BackupError("SQLite integrity_check 未通过")
            try:
                row = connection.execute(
                    "SELECT version_num FROM alembic_version LIMIT 1"
                ).fetchone()
            except sqlite3.Error as exc:
                raise BackupError("备份数据库缺少可读取的 Alembic revision") from exc
    except sqlite3.Error as exc:
        raise BackupError("备份数据库无法读取") from exc
    if row is None:
        return None
    revision = row[0]
    if not isinstance(revision, str) or not revision:
        raise BackupError("备份数据库 Alembic revision 无效")
    return revision


def _write_manifest(path: Path, artifact: BackupArtifact) -> None:
    payload = artifact.as_dict()
    payload.pop("manifest_file", None)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("备份 manifest 无法读取") from exc
    if not isinstance(payload, dict):
        raise BackupError("备份 manifest 顶层必须为对象")
    return dict(payload)


def _ensure_backup_directory(path: Path) -> Path:
    return _ensure_real_directory(path, label="备份目录")


def _ensure_real_directory(path: Path, *, label: str) -> Path:
    if path.is_symlink():
        raise BackupError(f"{label}不能是符号链接")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    mode = path.stat(follow_symlinks=False).st_mode
    if not stat.S_ISDIR(mode):
        raise BackupError(f"{label}必须是目录")
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise BackupError(f"{label}权限不足")
    os.chmod(path, 0o700)
    return path.resolve(strict=True)


def _require_existing_real_directory(path: Path, *, label: str) -> Path:
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except FileNotFoundError as exc:
        raise BackupError(f"{label}不存在") from exc
    if path.is_symlink() or not stat.S_ISDIR(mode):
        raise BackupError(f"{label}必须是真实目录且不能是符号链接")
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise BackupError(f"{label}权限不足")
    return path.resolve(strict=True)


def _require_regular_file(path: Path, *, label: str) -> None:
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except FileNotFoundError as exc:
        raise BackupError(f"{label}不存在") from exc
    if path.is_symlink() or not stat.S_ISREG(mode):
        raise BackupError(f"{label}必须是普通文件且不能是符号链接")


def _file_identity(path: Path, *, label: str) -> tuple[int, int]:
    _require_regular_file(path, label=label)
    metadata = path.stat(follow_symlinks=False)
    return metadata.st_dev, metadata.st_ino


def _delete_verified_backup_member(
    path: Path,
    identity: tuple[int, int],
    *,
    label: str,
) -> None:
    if path.suffix not in {".db", ".json"} or _BACKUP_STEM.fullmatch(path.stem) is None:
        raise BackupError(f"{label}名称不属于 PackBreaker 标准备份，拒绝删除")
    if _file_identity(path, label=label) != identity:
        raise BackupError(f"{label}在保留计划生成后发生变化，拒绝删除")
    path.unlink()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise BackupError(f"manifest {key} 无效")
    return value


def _parse_created_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupError("manifest created_at 无效") from exc
    if parsed.tzinfo is None:
        raise BackupError("manifest created_at 必须包含时区")
    return parsed.astimezone(UTC)


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
