from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from backend.app.config import AppSettings
from backend.app.infrastructure.backups import (
    BackupError,
    apply_backup_retention,
    create_consistent_backup,
    plan_backup_retention,
    restore_consistent_backup,
    verify_backup,
)
from backend.app.infrastructure.release_preflight import run_release_preflight
from backend.app.versioning import app_version


def _app_version() -> str:
    return app_version()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PackBreaker 离线/运维工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup", help="创建 SQLite 一致性备份")
    backup.add_argument(
        "--output-dir",
        type=Path,
        help="备份输出目录；默认 <PACKBREAKER_CONFIG_DIR>/backups",
    )

    verify = subparsers.add_parser("verify-backup", help="验证备份 manifest 与数据库完整性")
    verify.add_argument("database", type=Path, help="备份 .db 文件")
    verify.add_argument("--manifest", type=Path, help="manifest .json；默认与数据库同名")

    restore = subparsers.add_parser("restore-backup", help="离线恢复备份并保留恢复前安全快照")
    restore.add_argument("database", type=Path, help="备份 .db 文件")
    restore.add_argument("--manifest", type=Path, help="manifest .json；默认与数据库同名")
    restore.add_argument(
        "--safety-backup-dir",
        type=Path,
        help="恢复前安全快照目录；默认 <PACKBREAKER_CONFIG_DIR>/backups/pre-restore",
    )
    restore.add_argument(
        "--confirm-replace-current-database",
        action="store_true",
        help="确认替换当前数据库；活动 PackBreaker 实例仍会被锁门禁阻断",
    )

    retention = subparsers.add_parser("backup-retention", help="预览或执行普通备份保留策略")
    retention.add_argument("--retention-days", type=int, default=30, help="普通备份保留天数")
    retention.add_argument("--keep-latest", type=int, default=3, help="无条件保留最新备份数量")
    retention.add_argument("--backup-dir", type=Path, help="默认 <PACKBREAKER_CONFIG_DIR>/backups")
    retention.add_argument("--apply", action="store_true", help="执行计划；省略时只预览")
    retention.add_argument(
        "--confirm-delete-expired-backups",
        action="store_true",
        help="执行删除时必须显式确认",
    )

    preflight = subparsers.add_parser("preflight", help="执行不连接外部服务的发布前本地预检")
    preflight.add_argument(
        "--skip-backup-exercise",
        action="store_true",
        help="跳过一致性备份创建/验证演练；结果会带 warning",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "backup":
            settings = AppSettings()
            output_dir = args.output_dir or settings.config_dir / "backups"
            artifact = create_consistent_backup(
                settings.database_path,
                output_dir,
                app_version=_app_version(),
            )
        elif args.command == "verify-backup":
            database = args.database.resolve()
            manifest = args.manifest or database.with_suffix(".json")
            artifact = verify_backup(database, manifest.resolve())
        elif args.command == "restore-backup":
            if not args.confirm_replace_current_database:
                raise BackupError("恢复必须显式提供 --confirm-replace-current-database")
            settings = AppSettings()
            database = args.database.resolve()
            manifest = args.manifest or database.with_suffix(".json")
            safety_backup_dir = (
                args.safety_backup_dir or settings.config_dir / "backups" / "pre-restore"
            )
            result = restore_consistent_backup(
                database,
                manifest.resolve(),
                target_database_path=settings.database_path,
                instance_lock_path=settings.instance_lock_path,
                safety_backup_dir=safety_backup_dir,
                app_version=_app_version(),
            )
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "safety_backup_dir": str(safety_backup_dir.resolve()),
                        **result.as_dict(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        elif args.command == "backup-retention":
            settings = AppSettings()
            backup_dir = args.backup_dir or settings.config_dir / "backups"
            if args.apply:
                if not args.confirm_delete_expired_backups:
                    raise BackupError("执行备份清理必须显式提供 --confirm-delete-expired-backups")
                retention_result = apply_backup_retention(
                    backup_dir,
                    retention_days=args.retention_days,
                    keep_latest=args.keep_latest,
                )
                payload = {"status": "ok", "mode": "applied", **retention_result.as_dict()}
            else:
                retention_plan = plan_backup_retention(
                    backup_dir,
                    retention_days=args.retention_days,
                    keep_latest=args.keep_latest,
                )
                payload = {"status": "ok", "mode": "preview", **retention_plan.as_dict()}
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0
        else:
            settings = AppSettings()
            report = run_release_preflight(
                settings,
                app_version=_app_version(),
                exercise_backup=not args.skip_backup_exercise,
            )
            print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
            return 0 if report.ready else 2
    except (BackupError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2

    print(
        json.dumps(
            {
                "status": "ok",
                "backup_dir": str(artifact.database_path.parent),
                **artifact.as_dict(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
