from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.backup_schedule import BackupDriverState
from backend.app.application.history_scan_driver import HistoryScanDriverState
from backend.app.application.notification_driver import NotificationDriverState
from backend.app.application.task_driver import ActiveTaskDriverState
from backend.app.config import AppSettings
from backend.app.domain.notification import NotificationDeliveryState
from backend.app.domain.operation import OperationStatus
from backend.app.domain.task_state import TERMINAL_STATUSES, TaskStatus
from backend.app.infrastructure.backups import BackupError, plan_backup_retention
from backend.app.infrastructure.persistence.models import (
    BackupPolicy,
    Downloader,
    NotificationChannel,
    NotificationOutbox,
    OperationJournal,
    Site,
    UnpackTask,
)
from backend.app.infrastructure.runtime import RuntimeManager
from backend.app.infrastructure.site_reliability import SiteReliabilityRegistry
from backend.app.versioning import app_version

HealthStatus = Literal["ok", "warning", "blocked"]
MetricValue = str | int | float | bool | None

_BACKUP_STALE_AFTER = timedelta(days=7)
_TASK_STALE_AFTER = timedelta(hours=24)
_OPERATION_STALE_AFTER = timedelta(hours=1)
_LOW_DISK_RATIO = 0.10
_LOW_DISK_BYTES = 2 * 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SystemHealthCheck:
    name: str
    status: HealthStatus
    code: str
    detail: str
    metrics: dict[str, MetricValue]

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "code": self.code,
            "detail": self.detail,
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True, slots=True)
class SystemHealthReport:
    generated_at: datetime
    version: str
    checks: tuple[SystemHealthCheck, ...]

    @property
    def status(self) -> HealthStatus:
        statuses = {check.status for check in self.checks}
        if "blocked" in statuses:
            return "blocked"
        if "warning" in statuses:
            return "warning"
        return "ok"

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "generated_at": self.generated_at.isoformat().replace("+00:00", "Z"),
            "version": self.version,
            "checks": [check.as_dict() for check in self.checks],
        }


class SystemHealthService:
    """聚合现有本地证据；绝不为了健康检查主动连接外部依赖。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: AppSettings,
        runtime: RuntimeManager,
        site_reliability_registry: SiteReliabilityRegistry,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._runtime = runtime
        self._site_reliability_registry = site_reliability_registry

    async def snapshot(
        self,
        *,
        task_driver: ActiveTaskDriverState,
        history_driver: HistoryScanDriverState,
        notification_driver: NotificationDriverState,
        backup_driver: BackupDriverState,
        now: datetime | None = None,
    ) -> SystemHealthReport:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        with self._session_factory() as session:
            database = self._database_snapshot(session, now=timestamp)
        checks = [
            self._runtime_check(),
            self._storage_check(),
            self._backup_check(database, now=timestamp),
            self._task_check(database),
            self._operation_check(database),
            await self._site_check(database),
            self._downloader_check(database),
            self._notification_check(database),
            self._worker_check(task_driver, history_driver, notification_driver, backup_driver),
        ]
        return SystemHealthReport(timestamp, app_version(), tuple(checks))

    def _database_snapshot(self, session: Session, *, now: datetime) -> dict[str, object]:
        task_rows = session.execute(
            select(UnpackTask.status, func.count()).group_by(UnpackTask.status)
        ).all()
        operation_rows = session.execute(
            select(OperationJournal.status, func.count()).group_by(OperationJournal.status)
        ).all()
        stale_task_cutoff = now - _TASK_STALE_AFTER
        stale_operation_cutoff = now - _OPERATION_STALE_AFTER
        terminal = tuple(status.value for status in TERMINAL_STATUSES)
        unsettled_operations = (
            OperationStatus.INTENT_RECORDED.value,
            OperationStatus.ROLLBACK_PENDING.value,
            OperationStatus.RECONCILE_REQUIRED.value,
            OperationStatus.ROLLBACK_BLOCKED.value,
        )
        stale_tasks = int(
            session.scalar(
                select(func.count())
                .select_from(UnpackTask)
                .where(
                    UnpackTask.status.not_in(terminal), UnpackTask.updated_at < stale_task_cutoff
                )
            )
            or 0
        )
        stale_operations = int(
            session.scalar(
                select(func.count())
                .select_from(OperationJournal)
                .where(
                    OperationJournal.status.in_(unsettled_operations),
                    OperationJournal.updated_at < stale_operation_cutoff,
                )
            )
            or 0
        )
        dead_notifications = int(
            session.scalar(
                select(func.count())
                .select_from(NotificationOutbox)
                .where(NotificationOutbox.state == NotificationDeliveryState.DEAD.value)
            )
            or 0
        )
        return {
            "backup_policy": session.get(BackupPolicy, "default"),
            "tasks": {status: int(count) for status, count in task_rows},
            "operations": {status: int(count) for status, count in operation_rows},
            "stale_tasks": stale_tasks,
            "stale_operations": stale_operations,
            "sites": tuple(session.scalars(select(Site).order_by(Site.id))),
            "downloaders": tuple(session.scalars(select(Downloader).order_by(Downloader.id))),
            "notification_channels": tuple(
                session.scalars(select(NotificationChannel).order_by(NotificationChannel.id))
            ),
            "dead_notifications": dead_notifications,
        }

    def _runtime_check(self) -> SystemHealthCheck:
        report = self._runtime.readiness()
        return SystemHealthCheck(
            "runtime",
            "ok" if report.ready else "blocked",
            "RUNTIME_READY" if report.ready else "RUNTIME_NOT_READY",
            "本地数据库、migration、secret 与实例锁均就绪"
            if report.ready
            else "至少一个本地 readiness 安全门未通过",
            {
                "database_ok": report.database == "ok",
                "migrations_ok": report.migrations == "ok",
                "secrets_ok": report.secrets == "ok",
                "worker_slot_ok": report.worker_slot == "ok",
            },
        )

    def _storage_check(self) -> SystemHealthCheck:
        try:
            config = _disk_metrics(self._settings.config_dir)
            data = _disk_metrics(self._settings.data_dir)
        except OSError:
            return SystemHealthCheck(
                "storage",
                "blocked",
                "STORAGE_UNAVAILABLE",
                "配置卷或数据根无法读取磁盘容量信息",
                {},
            )
        low = bool(config["low_space"] or data["low_space"])
        return SystemHealthCheck(
            "storage",
            "warning" if low else "ok",
            "STORAGE_LOW" if low else "STORAGE_OK",
            "配置卷或数据卷剩余空间低于运维告警阈值" if low else "配置卷与数据卷空间正常",
            {
                "config_total_bytes": config["total_bytes"],
                "config_free_bytes": config["free_bytes"],
                "config_free_ratio": config["free_ratio"],
                "data_total_bytes": data["total_bytes"],
                "data_free_bytes": data["free_bytes"],
                "data_free_ratio": data["free_ratio"],
            },
        )

    def _backup_check(self, database: dict[str, object], *, now: datetime) -> SystemHealthCheck:
        backup_dir = self._settings.config_dir / "backups"
        try:
            plan = plan_backup_retention(
                backup_dir,
                retention_days=36500,
                keep_latest=10000,
                now=now,
            )
        except (BackupError, OSError):
            return SystemHealthCheck(
                "backups",
                "warning",
                "BACKUP_INSPECTION_FAILED",
                "普通备份目录无法安全检查；未读取 pre-restore/pre-upgrade 子目录",
                {},
            )
        valid = [
            item for item in plan.items if item.action != "blocked" and item.created_at is not None
        ]
        latest = max(
            (item.created_at for item in valid if item.created_at is not None), default=None
        )
        latest_age_hours = (
            None if latest is None else round((now - latest).total_seconds() / 3600, 3)
        )
        policy = _backup_policy_record(database["backup_policy"])
        schedule_enabled = policy.enabled if policy is not None else False
        last_schedule_success_age_hours = (
            None
            if policy is None or policy.last_success_at is None
            else round((now - policy.last_success_at).total_seconds() / 3600, 3)
        )
        schedule_overdue = bool(
            policy is not None
            and policy.enabled
            and (
                policy.last_success_at is None
                or now > policy.last_success_at + timedelta(hours=policy.interval_hours)
            )
        )
        schedule_error = None if policy is None else policy.last_error_code
        if plan.blocked_count:
            status: HealthStatus = "warning"
            code = "BACKUP_INVALID_PAIR"
            detail = "普通备份目录存在不完整或校验失败的标准备份 pair"
        elif latest is None:
            status = "warning"
            code = "BACKUP_MISSING"
            detail = "尚无可验证的普通一致性备份"
        elif now - latest > _BACKUP_STALE_AFTER:
            status = "warning"
            code = "BACKUP_STALE"
            detail = "最近一次可验证普通备份已超过 7 天"
        else:
            status = "ok"
            code = "BACKUP_OK"
            detail = "存在近期可验证的普通一致性备份"
        if schedule_enabled and (schedule_overdue or schedule_error is not None):
            if status == "ok":
                status = "warning"
                code = "BACKUP_SCHEDULE_ATTENTION"
                detail = "计划备份已启用，但最近执行逾期或存在保留/创建错误"
            else:
                detail += "；计划备份也存在逾期或错误状态"
        return SystemHealthCheck(
            "backups",
            status,
            code,
            detail,
            {
                "valid_backup_count": len(valid),
                "blocked_backup_count": plan.blocked_count,
                "latest_backup_age_hours": latest_age_hours,
                "schedule_enabled": schedule_enabled,
                "schedule_interval_hours": None if policy is None else policy.interval_hours,
                "schedule_overdue": schedule_overdue,
                "schedule_last_success_age_hours": last_schedule_success_age_hours,
                "schedule_last_error_code": schedule_error,
            },
        )

    def _task_check(self, database: dict[str, object]) -> SystemHealthCheck:
        by_status = _string_int_map(database["tasks"])
        terminal = {status.value for status in TERMINAL_STATUSES}
        active_count = sum(count for status, count in by_status.items() if status not in terminal)
        retry_count = by_status.get(TaskStatus.RETRY.value, 0)
        stale_count = _integer(database["stale_tasks"], label="stale_tasks")
        warning = retry_count > 0 or stale_count > 0
        return SystemHealthCheck(
            "tasks",
            "warning" if warning else "ok",
            "TASK_BACKLOG_ATTENTION" if warning else "TASK_BACKLOG_OK",
            "存在 RETRY 或超过 24 小时未更新的非终态任务"
            if warning
            else "未发现需要运维关注的任务积压",
            {
                "total": sum(by_status.values()),
                "active": active_count,
                "retry": retry_count,
                "stale_active": stale_count,
                "awaiting_confirmation": by_status.get(TaskStatus.AWAITING_CONFIRMATION.value, 0),
            },
        )

    def _operation_check(self, database: dict[str, object]) -> SystemHealthCheck:
        by_status = _string_int_map(database["operations"])
        reconcile = by_status.get(OperationStatus.RECONCILE_REQUIRED.value, 0)
        blocked = by_status.get(OperationStatus.ROLLBACK_BLOCKED.value, 0)
        stale = _integer(database["stale_operations"], label="stale_operations")
        warning = reconcile > 0 or blocked > 0 or stale > 0
        return SystemHealthCheck(
            "operations",
            "warning" if warning else "ok",
            "OPERATION_ATTENTION_REQUIRED" if warning else "OPERATION_OK",
            "存在需要只读对账、人工检查或长时间未收敛的 operation journal"
            if warning
            else "未发现需要运维关注的 operation journal",
            {
                "total": sum(by_status.values()),
                "reconcile_required": reconcile,
                "rollback_blocked": blocked,
                "stale_unsettled": stale,
            },
        )

    async def _site_check(self, database: dict[str, object]) -> SystemHealthCheck:
        sites = _site_records(database["sites"])
        enabled = [site for site in sites if site.enabled]
        failed = sum(site.connection_status == "FAILED" for site in enabled)
        untested = sum(site.connection_status == "UNTESTED" for site in enabled)
        open_circuits = 0
        half_open_circuits = 0
        for site in enabled:
            health = await self._site_reliability_registry.health(
                config_id=site.id,
                config_version=site.version,
            )
            open_circuits += health.circuit_state == "OPEN"
            half_open_circuits += health.circuit_state == "HALF_OPEN"
        warning = failed > 0 or untested > 0 or open_circuits > 0 or half_open_circuits > 0
        return SystemHealthCheck(
            "sites",
            "warning" if warning else "ok",
            "SITE_ATTENTION_REQUIRED" if warning else "SITE_OK",
            "已启用站点存在失败/未测试连接或熔断状态"
            if warning
            else "已启用站点的已有健康证据正常",
            {
                "configured": len(sites),
                "enabled": len(enabled),
                "connection_failed": failed,
                "connection_untested": untested,
                "circuit_open": open_circuits,
                "circuit_half_open": half_open_circuits,
            },
        )

    def _downloader_check(self, database: dict[str, object]) -> SystemHealthCheck:
        downloaders = _downloader_records(database["downloaders"])
        enabled = [downloader for downloader in downloaders if downloader.enabled]
        connection_failed = sum(item.connection_status == "FAILED" for item in enabled)
        connection_untested = sum(item.connection_status == "UNTESTED" for item in enabled)
        mapping_failed = sum(item.path_mapping_status == "FAILED" for item in enabled)
        mapping_untested = sum(item.path_mapping_status == "UNTESTED" for item in enabled)
        warning = any((connection_failed, connection_untested, mapping_failed, mapping_untested))
        return SystemHealthCheck(
            "downloaders",
            "warning" if warning else "ok",
            "DOWNLOADER_ATTENTION_REQUIRED" if warning else "DOWNLOADER_OK",
            "已启用下载器存在失败/未测试的连接或路径映射证据"
            if warning
            else "已启用下载器的已有连接与路径证据正常",
            {
                "configured": len(downloaders),
                "enabled": len(enabled),
                "connection_failed": connection_failed,
                "connection_untested": connection_untested,
                "path_mapping_failed": mapping_failed,
                "path_mapping_untested": mapping_untested,
            },
        )

    def _notification_check(self, database: dict[str, object]) -> SystemHealthCheck:
        channels = _notification_records(database["notification_channels"])
        enabled = [channel for channel in channels if channel.enabled]
        unhealthy = sum(channel.connection_status != "OK" for channel in enabled)
        dead = _integer(database["dead_notifications"], label="dead_notifications")
        warning = unhealthy > 0 or dead > 0
        return SystemHealthCheck(
            "notifications",
            "warning" if warning else "ok",
            "NOTIFICATION_ATTENTION_REQUIRED" if warning else "NOTIFICATION_OK",
            "已启用通知渠道存在非 OK 探测结果或 DEAD 投递"
            if warning
            else "通知渠道与投递队列无已知异常",
            {
                "configured": len(channels),
                "enabled": len(enabled),
                "enabled_not_ok": unhealthy,
                "dead_deliveries": dead,
            },
        )

    def _worker_check(
        self,
        task: ActiveTaskDriverState,
        history: HistoryScanDriverState,
        notification: NotificationDriverState,
        backup: BackupDriverState,
    ) -> SystemHealthCheck:
        running = task.running and history.running and notification.running and backup.running
        errors = (
            task.consecutive_errors
            + history.consecutive_errors
            + notification.consecutive_errors
            + backup.consecutive_errors
        )
        if not running:
            status: HealthStatus = "blocked"
            code = "BACKGROUND_WORKER_STOPPED"
            detail = "至少一个后台 driver 未运行"
        elif errors:
            status = "warning"
            code = "BACKGROUND_WORKER_ERRORS"
            detail = "至少一个后台 driver 存在连续错误"
        else:
            status = "ok"
            code = "BACKGROUND_WORKERS_OK"
            detail = "任务、历史扫描、通知和计划备份 driver 均在运行且无连续错误"
        return SystemHealthCheck(
            "workers",
            status,
            code,
            detail,
            {
                "task_running": task.running,
                "history_running": history.running,
                "notification_running": notification.running,
                "backup_running": backup.running,
                "task_consecutive_errors": task.consecutive_errors,
                "history_consecutive_errors": history.consecutive_errors,
                "notification_consecutive_errors": notification.consecutive_errors,
                "backup_consecutive_errors": backup.consecutive_errors,
            },
        )


def _disk_metrics(path: Path) -> dict[str, int | float | bool]:
    usage = shutil.disk_usage(path)
    ratio = 0.0 if usage.total == 0 else usage.free / usage.total
    return {
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "free_ratio": round(ratio, 6),
        "low_space": usage.free < _LOW_DISK_BYTES or ratio < _LOW_DISK_RATIO,
    }


def _integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    return value


def _string_int_map(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        raise TypeError("health database snapshot map invalid")
    return {str(key): _integer(item, label="health counter") for key, item in value.items()}


def _backup_policy_record(value: object) -> BackupPolicy | None:
    if value is not None and not isinstance(value, BackupPolicy):
        raise TypeError("health backup policy snapshot invalid")
    return value


def _site_records(value: object) -> tuple[Site, ...]:
    if not isinstance(value, tuple) or not all(isinstance(item, Site) for item in value):
        raise TypeError("health site snapshot invalid")
    return value


def _downloader_records(value: object) -> tuple[Downloader, ...]:
    if not isinstance(value, tuple) or not all(isinstance(item, Downloader) for item in value):
        raise TypeError("health downloader snapshot invalid")
    return value


def _notification_records(value: object) -> tuple[NotificationChannel, ...]:
    if not isinstance(value, tuple) or not all(
        isinstance(item, NotificationChannel) for item in value
    ):
        raise TypeError("health notification snapshot invalid")
    return value
