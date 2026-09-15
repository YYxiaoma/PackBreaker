from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from alembic.script import ScriptDirectory

from backend.app.config import AppSettings
from backend.app.infrastructure.backups import (
    BackupError,
    create_consistent_backup,
    validate_sqlite_database,
    verify_backup,
)
from backend.app.infrastructure.persistence.database import sqlite_database_url
from backend.app.infrastructure.runtime import make_migration_config
from backend.app.infrastructure.security import MasterKeyFile, SecretCipher, SecretKeyError


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    name: str
    status: Literal["ok", "warning", "blocked"]
    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "code": self.code,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ReleasePreflightReport:
    app_version: str
    checks: tuple[PreflightCheck, ...]

    @property
    def ready(self) -> bool:
        return all(check.status != "blocked" for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "ready" if self.ready else "blocked",
            "app_version": self.app_version,
            "checks": [check.as_dict() for check in self.checks],
        }


def run_release_preflight(
    settings: AppSettings,
    *,
    app_version: str,
    exercise_backup: bool = True,
) -> ReleasePreflightReport:
    """执行不访问外部服务、不遍历媒体树的发布前本地安全预检。"""

    checks: list[PreflightCheck] = []
    config_ok = _check_config_directory(settings.config_dir, checks)
    database_ok = _check_database(settings.database_path, checks)
    _check_secret(settings.resolved_secret_key_file, checks)
    _check_data_root(settings.data_dir, checks)
    _check_docker_socket(checks)

    if exercise_backup and config_ok and database_ok:
        _exercise_backup(settings, app_version=app_version, checks=checks)
    elif exercise_backup:
        checks.append(
            PreflightCheck(
                "backup_exercise",
                "blocked",
                "BACKUP_EXERCISE_PREREQUISITE_FAILED",
                "配置目录或数据库检查未通过，未执行备份演练",
            )
        )
    else:
        checks.append(
            PreflightCheck(
                "backup_exercise",
                "warning",
                "BACKUP_EXERCISE_SKIPPED",
                "已按显式参数跳过一致性备份创建/验证演练",
            )
        )

    return ReleasePreflightReport(app_version=app_version, checks=tuple(checks))


def _check_config_directory(path: Path, checks: list[PreflightCheck]) -> bool:
    try:
        metadata = path.stat(follow_symlinks=False)
        if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("配置目录不是安全的真实目录")
        if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
            raise RuntimeError("配置目录不可读写")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RuntimeError("配置目录存在 group/world 权限")
    except (OSError, RuntimeError) as exc:
        checks.append(PreflightCheck("config_dir", "blocked", "CONFIG_DIR_UNSAFE", str(exc)))
        return False
    checks.append(PreflightCheck("config_dir", "ok", "CONFIG_DIR_OK", "配置目录权限与可写性通过"))
    return True


def _check_database(path: Path, checks: list[PreflightCheck]) -> bool:
    try:
        current_revision = validate_sqlite_database(path)
        expected_revision = ScriptDirectory.from_config(
            make_migration_config(sqlite_database_url(path))
        ).get_current_head()
        if current_revision != expected_revision:
            raise BackupError("当前数据库 Alembic revision 不是发布代码 head")
    except (BackupError, OSError, RuntimeError) as exc:
        checks.append(PreflightCheck("database", "blocked", "DATABASE_NOT_READY", str(exc)))
        return False
    checks.append(
        PreflightCheck(
            "database",
            "ok",
            "DATABASE_OK",
            f"SQLite integrity_check 与 migration head 通过（{current_revision}）",
        )
    )
    return True


def _check_secret(path: Path, checks: list[PreflightCheck]) -> None:
    try:
        key = MasterKeyFile.load_existing(path)
        if not SecretCipher(key).self_test():
            raise SecretKeyError("secret 加解密自检失败")
    except (SecretKeyError, OSError) as exc:
        checks.append(PreflightCheck("secret_key", "blocked", "SECRET_KEY_UNSAFE", str(exc)))
        return
    checks.append(PreflightCheck("secret_key", "ok", "SECRET_KEY_OK", "主密钥存在且安全自检通过"))


def _check_data_root(path: Path, checks: list[PreflightCheck]) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
        if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("数据根目录不是安全的真实目录")
        if not os.access(path, os.R_OK | os.X_OK):
            raise RuntimeError("数据根目录不可读取/遍历")
    except (OSError, RuntimeError) as exc:
        checks.append(PreflightCheck("data_root", "blocked", "DATA_ROOT_UNAVAILABLE", str(exc)))
        return
    checks.append(
        PreflightCheck(
            "data_root",
            "ok",
            "DATA_ROOT_OK",
            "数据根目录可见；预检未遍历或修改任何媒体文件",
        )
    )


def _check_docker_socket(checks: list[PreflightCheck]) -> None:
    if Path("/var/run/docker.sock").exists():
        checks.append(
            PreflightCheck(
                "docker_socket",
                "warning",
                "DOCKER_SOCKET_PRESENT",
                (
                    "主服务检测到 docker.sock；自动升级应只由独立 updater helper 持有，"
                    "建议移除主容器挂载"
                ),
            )
        )
        return
    checks.append(
        PreflightCheck(
            "docker_socket",
            "ok",
            "DOCKER_SOCKET_ABSENT",
            "主服务未挂载 docker.sock；自动升级可由独立 updater helper 安全接管",
        )
    )


def _exercise_backup(
    settings: AppSettings,
    *,
    app_version: str,
    checks: list[PreflightCheck],
) -> None:
    backup_root = settings.config_dir / "backups"
    exercise_dir = backup_root / "preflight"
    backup_root_existed = backup_root.exists()
    artifact = None
    free_bytes: int | None = None
    try:
        artifact = create_consistent_backup(
            settings.database_path,
            exercise_dir,
            app_version=app_version,
        )
        verified = verify_backup(artifact.database_path, artifact.manifest_path)
        if verified.database_sha256 != artifact.database_sha256:
            raise BackupError("备份演练摘要复验不一致")
        free_bytes = shutil.disk_usage(settings.config_dir).free
    except (BackupError, OSError) as exc:
        checks.append(
            PreflightCheck("backup_exercise", "blocked", "BACKUP_EXERCISE_FAILED", str(exc))
        )
        return
    finally:
        if artifact is not None:
            artifact.database_path.unlink(missing_ok=True)
            artifact.manifest_path.unlink(missing_ok=True)
        try:
            exercise_dir.rmdir()
            if not backup_root_existed:
                backup_root.rmdir()
        except OSError:
            pass

    checks.append(
        PreflightCheck(
            "backup_exercise",
            "ok",
            "BACKUP_EXERCISE_OK",
            f"一致性备份创建/验证/清理通过；配置卷剩余空间 {free_bytes} bytes",
        )
    )
